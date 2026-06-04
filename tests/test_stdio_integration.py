# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""End-to-end stdio loop integration test.

Spawns `python -m clawtouch_mcp --mock` as a real subprocess, feeds it
JSON-RPC over stdin, and asserts the responses on stdout. This catches
platform-specific bugs in the stdio reader that pure unit tests miss —
notably the Windows ProactorEventLoop + asyncio.connect_read_pipe issue
that left v0.2.0 / v0.2.1 unusable on Windows for ~zero outside
discovery before the first publish (host MCP clients couldn't even
complete the initialize handshake).

Runs on every platform in CI; the bug only reproduces on Windows but the
test is cheap to run everywhere and guards against regressing other
platforms too.
"""
from __future__ import annotations

import json
import subprocess
import sys



TIMEOUT_SECS = 15


def _spawn_and_exchange(requests: list[dict],
                        extra_args: list[str] | None = None) -> tuple[list[dict], str]:
    """Spawn the server, send all requests as line-delimited JSON, close
    stdin, wait for it to exit on EOF, and return (parsed_responses,
    stderr_text)."""
    args = [sys.executable, "-m", "clawtouch_mcp", "--mock",
            "--log-level", "ERROR"]
    if extra_args:
        args.extend(extra_args)
    blob = ("\n".join(json.dumps(r) for r in requests) + "\n").encode("utf-8")
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out_b, err_b = proc.communicate(input=blob, timeout=TIMEOUT_SECS)
    out = out_b.decode("utf-8", errors="replace")
    err = err_b.decode("utf-8", errors="replace")
    responses: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        responses.append(json.loads(line))
    return responses, err


def _initialize_req(req_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0", "id": req_id, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }


def _initialized_notif() -> dict:
    return {"jsonrpc": "2.0", "method": "notifications/initialized"}


class TestStdioInitializeHandshake:
    """Cover the very first message — this is what failed on Windows."""

    def test_initialize_returns_server_info(self):
        responses, _ = _spawn_and_exchange([_initialize_req(1)])
        assert len(responses) == 1
        r = responses[0]
        assert r["id"] == 1
        assert "result" in r, f"expected result, got {r}"
        assert r["result"]["protocolVersion"] == "2024-11-05"
        assert r["result"]["serverInfo"]["name"] == "clawtouch-mcp"

    def test_initialize_then_tools_list(self):
        responses, _ = _spawn_and_exchange([
            _initialize_req(1),
            _initialized_notif(),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        # initialize response + tools/list response (notification has no response)
        assert len(responses) == 2
        tools_resp = next(r for r in responses if r.get("id") == 2)
        tools = tools_resp["result"]["tools"]
        names = {t["name"] for t in tools}
        # 9 v1.0 baseline + 6 v1.1 additions + hid.batch (v0.4.0)
        # (hid.screenshot is opt-in via --allow-screenshot, tested in
        # TestStdioScreenshotOptIn)
        expected_baseline = {
            # v1.0
            "hid.click", "hid.move", "hid.hover", "hid.type", "hid.scroll",
            "hid.key", "hid.release_all", "device.list", "device.info",
            # v1.1
            "hid.mouse_button_down", "hid.mouse_button_up", "hid.drag",
            "hid.key_press", "hid.key_release", "hid.hold_key",
            # v0.4.0
            "hid.batch",
        }
        assert names == expected_baseline, f"unexpected tools: {names}"


class TestStdioToolCall:
    def test_hid_click_mock(self):
        responses, _ = _spawn_and_exchange([
            _initialize_req(1),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "hid.click",
                        "arguments": {"x": 100, "y": 200, "button": "left"}}},
        ])
        click_resp = next(r for r in responses if r.get("id") == 2)
        assert "result" in click_resp
        # content is a list of text blocks per MCP spec
        text = click_resp["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert payload["ok"] is True
        assert payload["x"] == 100
        assert payload["y"] == 200

    def test_hid_type_in_mock(self):
        responses, _ = _spawn_and_exchange([
            _initialize_req(1),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "hid.type",
                        "arguments": {"text": "hello"}}},
        ])
        type_resp = next(r for r in responses if r.get("id") == 2)
        assert "result" in type_resp
        assert type_resp["result"]["isError"] is False


class TestStdioScreenshotOptIn:
    def test_screenshot_visible_with_flag(self):
        responses, _ = _spawn_and_exchange(
            [_initialize_req(1),
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}],
            extra_args=["--allow-screenshot"],
        )
        tools_resp = next(r for r in responses if r.get("id") == 2)
        names = {t["name"] for t in tools_resp["result"]["tools"]}
        assert "hid.screenshot" in names, f"missing screenshot in {names}"
        # 16 baseline (9 v1.0 + 6 v1.1 + hid.batch) + 1 opt-in screenshot
        assert len(names) == 17


class TestStdioStability:
    def test_eof_clean_shutdown(self):
        # No request, just close stdin. Server should exit cleanly.
        proc = subprocess.Popen(
            [sys.executable, "-m", "clawtouch_mcp", "--mock", "--log-level", "ERROR"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out, err = proc.communicate(input=b"", timeout=TIMEOUT_SECS)
        # Either 0 (clean exit) or any reasonable non-crash code is fine,
        # what matters is that it did not hang and did not crash with a traceback.
        assert b"Traceback" not in err, f"server crashed:\n{err.decode('utf-8', 'replace')}"

    def test_unknown_method_returns_error_not_crash(self):
        responses, err = _spawn_and_exchange([
            _initialize_req(1),
            {"jsonrpc": "2.0", "id": 99, "method": "totally/bogus"},
        ])
        err_resp = next(r for r in responses if r.get("id") == 99)
        assert "error" in err_resp
        assert err_resp["error"]["code"] == -32601  # method not found
        assert "Traceback" not in err

    def test_non_object_json_does_not_crash_session(self):
        # A single valid-but-non-object JSON line ([] / "x") used to raise
        # AttributeError in dispatch() (msg.get before the try) and take the
        # whole stdio loop down, dropping every subsequent message. It must now
        # come back as -32600 Invalid Request and leave the session alive.
        responses, err = _spawn_and_exchange([
            _initialize_req(1),
            [],  # valid JSON, not a JSON-RPC object
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ])
        assert "Traceback" not in err, err
        # The trailing ping proves the session survived the bad line.
        ping_resp = next((r for r in responses if r.get("id") == 2), None)
        assert ping_resp is not None and ping_resp.get("result") == {}, responses
        # The bad line itself came back as Invalid Request (-32600, id null).
        assert any(r.get("error", {}).get("code") == -32600 for r in responses), responses
