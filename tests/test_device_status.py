# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Beijing Tinqiao Technology Co., Ltd.
"""device.status: what the board is, asked without pressing anything.

What is pinned here:

  - the bridge sends exactly one STATUS_REQUEST (opcode 0xF0, empty
    payload) and parses the firmware's JSON answer; anything else — no
    answer, a firmware ERROR, a different frame, text that is not a JSON
    object — is None with the reason in last_error_detail;
  - the tool answers every outcome (mock, no board, board silent, board
    unplugged mid-call, board answering) as data, never as a tool error;
  - reconnecting a released port and asking STATUS sends no HID report:
    the only frame on the wire is the STATUS_REQUEST;
  - abs_pointer says whether moves will start with an absolute hop, and
    why not, in the same order _abs_hop checks.
"""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from clawtouch_mcp import server as server_mod
from clawtouch_mcp.bridge import SerialHidBridge
from clawtouch_mcp.protocol import PROTOCOL_VERSION, CommandType, ErrorCode
from clawtouch_mcp.server import ClawTouchMcpServer, MockBridge, ServerConfig, UnavailableBridge

FW_12 = {"fw_ver": "1.2.0", "board": "Raspberry Pi Pico 2", "uptime_ms": 1234, "caps": ["abs"]}
FW_11 = {"fw_ver": "1.1.0", "board": "Raspberry Pi Pico 2", "uptime_ms": 99}


def _status_frame(obj):
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
    return SimpleNamespace(cmd_type=CommandType.STATUS_RESPONSE, payload=raw)


def _bridge_answering(*responses):
    bridge = SerialHidBridge("COM_TEST", baudrate=115200)
    sent = []
    queue = list(responses)

    async def fake_send_raw(cmd, **_kw):
        sent.append(cmd)
        return queue.pop(0)

    bridge._send_raw = fake_send_raw
    return bridge, sent


def _server(bridge=None, **cfg):
    cfg.setdefault("screen_w", 1920)
    cfg.setdefault("screen_h", 1080)
    srv = ClawTouchMcpServer(ServerConfig(**cfg))
    srv.bridge = bridge
    return srv


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    # what _abs_hop reads too; headless CI has no screen to detect
    monkeypatch.setattr(server_mod, "_detect_screen", lambda: (5120, 1440))


# ── bridge ──────────────────────────────────────────────────────────


def test_bridge_sends_one_bare_status_request_and_parses_the_answer():
    bridge, sent = _bridge_answering(_status_frame(FW_12))
    assert asyncio.run(bridge.firmware_status()) == FW_12
    assert len(sent) == 1
    assert sent[0].cmd_type == CommandType.STATUS_REQUEST
    assert sent[0].payload == b""


def test_bridge_no_answer_is_none():
    bridge, _ = _bridge_answering(None)
    assert asyncio.run(bridge.firmware_status()) is None


def test_bridge_firmware_error_keeps_the_firmware_reason():
    bridge, _ = _bridge_answering(
        SimpleNamespace(cmd_type=CommandType.ERROR, payload=bytes([ErrorCode.UNKNOWN_COMMAND])))
    bridge._last_error_detail = "firmware ERROR UNKNOWN_COMMAND"  # as _send_raw sets it
    assert asyncio.run(bridge.firmware_status()) is None
    assert bridge.last_error_detail == "firmware ERROR UNKNOWN_COMMAND"


def test_bridge_a_different_frame_is_none_with_a_reason():
    bridge, _ = _bridge_answering(SimpleNamespace(cmd_type=CommandType.ACK, payload=b""))
    assert asyncio.run(bridge.firmware_status()) is None
    assert "STATUS_RESPONSE" in bridge.last_error_detail


@pytest.mark.parametrize("payload", [b"not json", b"\xff\xfe", b"[1, 2]", b'"text"'])
def test_bridge_an_answer_that_is_not_a_json_object_is_none(payload):
    bridge, _ = _bridge_answering(_status_frame(payload))
    assert asyncio.run(bridge.firmware_status()) is None
    assert bridge.last_error_detail


# ── tool ────────────────────────────────────────────────────────────


def test_tool_is_registered_and_says_it_presses_nothing():
    srv = _server(mock=True)
    tool = srv.tools["device.status"]
    assert "no HID report" in tool.description


def test_mock_reports_mock_and_touches_nothing():
    bridge = MockBridge()
    srv = _server(bridge, mock=True)
    out = asyncio.run(srv._tool_device_status())
    assert out["mode"] == "mock"
    assert out["connected"] is False
    assert out["abs_pointer"]["enabled"] is False
    assert out["mcp_version"] == server_mod.__version__
    assert out["protocol_version"] == PROTOCOL_VERSION
    assert bridge._calls == []


def test_no_board_is_an_answer_not_an_error(monkeypatch):
    monkeypatch.setattr(server_mod, "auto_detect_ports", lambda: [])
    srv = _server()
    srv.bridge = UnavailableBridge(srv, [], 115200)
    out = asyncio.run(srv._tool_device_status())
    assert out["mode"] == "serial"
    assert out["connected"] is False
    assert "No ClawTouch HID board was found" in out["detail"]
    assert out["abs_pointer"] == {"firmware": False, "enabled": False, "reason": "no board connected"}


def test_busy_port_names_the_ports_tried(monkeypatch):
    class Busy(SerialHidBridge):
        async def connect(self):
            raise OSError("access denied")

    monkeypatch.setattr(server_mod, "auto_detect_ports", lambda: ["COM9"])
    monkeypatch.setattr(server_mod, "SerialHidBridge", Busy)
    srv = _server()
    srv.bridge = UnavailableBridge(srv, ["COM9"], 115200)
    out = asyncio.run(srv._tool_device_status())
    assert out["connected"] is False
    assert out["tried_ports"] == ["COM9"]
    assert "busy" in out["detail"]


def test_reconnect_then_status_sends_only_the_status_request(monkeypatch, win):
    """The page's check after the idle release: the port is reopened and
    STATUS asked. Nothing else goes on the wire — no release_all, no move."""
    sent = []

    class Fake(SerialHidBridge):
        async def connect(self):
            self._serial = SimpleNamespace(is_open=True, close=lambda: None)

        async def _send_raw(self, cmd, **_kw):
            sent.append(cmd.cmd_type)
            return _status_frame(FW_12)

    monkeypatch.setattr(server_mod, "auto_detect_ports", lambda: ["COM9"])
    monkeypatch.setattr(server_mod, "SerialHidBridge", Fake)
    srv = _server(idle_close_after=0)
    srv.bridge = UnavailableBridge(srv, ["COM9"], 115200)
    out = asyncio.run(srv._tool_device_status())
    assert out["connected"] is True and out["responding"] is True
    assert out["port"] == "COM9"
    assert out["firmware"] == {"version": "1.2.0", "board": "Raspberry Pi Pico 2", "caps": ["abs"]}
    assert sent == [CommandType.STATUS_REQUEST]
    assert isinstance(srv.bridge, Fake)


def test_firmware_12_on_windows_uses_the_absolute_pointer(win):
    bridge, sent = _bridge_answering(_status_frame(FW_12))
    out = asyncio.run(_server(bridge)._tool_device_status())
    assert out["abs_pointer"] == {"firmware": True, "enabled": True, "reason": None}
    assert [c.cmd_type for c in sent] == [CommandType.STATUS_REQUEST]


def test_older_firmware_says_why_there_is_no_absolute_pointer(win):
    bridge, _ = _bridge_answering(_status_frame(FW_11))
    out = asyncio.run(_server(bridge)._tool_device_status())
    assert out["firmware"]["version"] == "1.1.0"
    assert out["firmware"]["caps"] == []
    assert out["abs_pointer"]["firmware"] is False
    assert out["abs_pointer"]["enabled"] is False
    assert "1.2" in out["abs_pointer"]["reason"]


def test_no_abs_pointer_flag_is_the_reason(win):
    bridge, _ = _bridge_answering(_status_frame(FW_12))
    out = asyncio.run(_server(bridge, abs_pointer=False)._tool_device_status())
    assert out["abs_pointer"]["firmware"] is True
    assert out["abs_pointer"]["enabled"] is False
    assert "--no-abs-pointer" in out["abs_pointer"]["reason"]


def test_off_windows_the_hop_is_not_used(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    bridge, _ = _bridge_answering(_status_frame(FW_12))
    out = asyncio.run(_server(bridge)._tool_device_status())
    assert out["abs_pointer"]["firmware"] is True
    assert out["abs_pointer"]["enabled"] is False
    assert "Windows" in out["abs_pointer"]["reason"]


def test_silent_board_is_connected_but_not_responding(win):
    bridge, _ = _bridge_answering(None)
    bridge._last_error_detail = "ACK timeout"
    out = asyncio.run(_server(bridge)._tool_device_status())
    assert out["connected"] is True
    assert out["responding"] is False
    assert out["detail"] == "ACK timeout"


def test_unplugged_mid_call_is_an_answer(win):
    bridge = SerialHidBridge("COM_TEST", baudrate=115200)

    async def boom(cmd, **_kw):
        raise OSError("device disconnected")

    bridge._send_raw = boom
    out = asyncio.run(_server(bridge)._tool_device_status())
    assert out["responding"] is False
    assert "device disconnected" in out["detail"]


def test_through_the_tool_call_path_it_is_not_an_error(win):
    bridge, _ = _bridge_answering(_status_frame(FW_12))
    srv = _server(bridge)
    result = asyncio.run(srv._on_tool_call({"name": "device.status", "arguments": {}}))
    assert not result.get("isError")
    body = json.loads(result["content"][0]["text"])
    assert body["firmware"]["version"] == "1.2.0"


def test_port_enumeration_failure_is_an_answer(monkeypatch):
    def broken():
        raise OSError("enumeration failed")

    monkeypatch.setattr(server_mod, "auto_detect_ports", broken)
    srv = _server()
    srv.bridge = UnavailableBridge(srv, [], 115200)
    result = asyncio.run(srv._on_tool_call({"name": "device.status", "arguments": {}}))
    assert not result.get("isError")
    body = json.loads(result["content"][0]["text"])
    assert body["connected"] is False
    assert "could not list serial ports" in body["detail"]


@pytest.mark.parametrize("primary, words", [
    (None, "could not be read"),
    ((32768, 1440), "32768"),
])
def test_abs_pointer_off_where_the_hop_would_skip(monkeypatch, primary, words):
    """Reported the way _abs_hop decides: no hop is sent in these cases."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(server_mod, "_detect_screen", lambda: primary)
    bridge, _ = _bridge_answering(_status_frame(FW_12))
    out = asyncio.run(_server(bridge)._tool_device_status())
    assert out["abs_pointer"]["firmware"] is True
    assert out["abs_pointer"]["enabled"] is False
    assert words in out["abs_pointer"]["reason"]
