# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""JSON-RPC 2.0 conformance for dispatch (0.4.3 audit fixes).

- Never reply to a Notification, even an *unhandled* one (§4.1). Previously an
  unknown-method notification fell through to the `-32601` error path and the
  server emitted a spurious `id:null` error a client can't correlate.
- Non-dict `params` → `-32602 Invalid params`, not `-32603 Internal error`
  with the raw Python `'list' object has no attribute 'get'` leaked.
"""
from __future__ import annotations

import asyncio

from clawtouch_mcp.server import ClawTouchMcpServer, MockBridge, ServerConfig


def _srv():
    s = ClawTouchMcpServer(ServerConfig(mock=True))
    s.bridge = MockBridge()
    return s


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestNotificationSuppression:
    def test_unknown_notification_gets_no_reply(self):
        # No `id` → Notification. Unhandled method must NOT produce a response.
        out = _run(_srv().dispatch(
            {"jsonrpc": "2.0", "method": "notifications/progress"}))
        assert out is None

    def test_future_notification_gets_no_reply(self):
        out = _run(_srv().dispatch(
            {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"}))
        assert out is None

    def test_known_notification_still_silent(self):
        out = _run(_srv().dispatch(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        assert out is None

    def test_unknown_request_still_gets_method_not_found(self):
        # WITH an id it's a request → -32601 is correct and expected.
        out = _run(_srv().dispatch(
            {"jsonrpc": "2.0", "id": 5, "method": "no.such.method"}))
        assert out["id"] == 5
        assert out["error"]["code"] == -32601


class TestParamsValidation:
    def test_non_dict_params_request_is_invalid_params(self):
        out = _run(_srv().dispatch({
            "jsonrpc": "2.0", "id": 7,
            "method": "tools/call", "params": ["not", "a", "dict"],
        }))
        assert out["error"]["code"] == -32602
        # The raw Python AttributeError must not leak.
        assert "has no attribute" not in out["error"]["message"]

    def test_string_params_request_is_invalid_params(self):
        out = _run(_srv().dispatch({
            "jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": "hi",
        }))
        assert out["error"]["code"] == -32602

    def test_non_dict_params_notification_no_reply(self):
        # Malformed params on a Notification → still no reply.
        out = _run(_srv().dispatch(
            {"jsonrpc": "2.0", "method": "tools/call", "params": "bad"}))
        assert out is None

    def test_missing_params_defaults_to_empty_dict(self):
        # ping takes no params; omitting params must not 400.
        out = _run(_srv().dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
        assert out["id"] == 1 and "result" in out

    def test_valid_dict_params_not_rejected(self):
        # Unknown tool with well-formed params → tool error (result+isError),
        # NOT a -32602 params rejection.
        out = _run(_srv().dispatch({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "nope.tool", "arguments": {}},
        }))
        assert out.get("error", {}).get("code") != -32602
