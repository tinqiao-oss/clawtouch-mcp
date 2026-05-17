"""ClawTouchMcpServer dispatch / tool registry / safety tests.

Uses MockBridge so no hardware is touched. Covers:
- 9 baseline tools registered (10 if --allow-screenshot)
- JSON-RPC dispatch: initialize / tools/list / tools/call / unknown method
- Coordinate clamping when --screen given
- Type length cap
"""
from __future__ import annotations

import asyncio
import json

import pytest

from clawtouch_mcp.server import (
    MAX_TYPE_LEN,
    ClawTouchMcpServer,
    MockBridge,
    ServerConfig,
)


@pytest.fixture
def server():
    cfg = ServerConfig(screen_w=1920, screen_h=1080, mock=True)
    srv = ClawTouchMcpServer(cfg)
    srv.bridge = MockBridge()
    return srv


@pytest.fixture
def server_with_screenshot():
    cfg = ServerConfig(screen_w=1920, screen_h=1080, mock=True,
                       allow_screenshot=True)
    srv = ClawTouchMcpServer(cfg)
    srv.bridge = MockBridge()
    return srv


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class TestToolRegistry:
    def test_baseline_9_tools(self, server):
        names = set(server.tools.keys())
        expected = {
            "hid.click", "hid.move", "hid.hover", "hid.type",
            "hid.scroll", "hid.key", "hid.release_all",
            "device.list", "device.info",
        }
        assert names == expected

    def test_screenshot_tool_opt_in(self, server, server_with_screenshot):
        assert "hid.screenshot" not in server.tools
        assert "hid.screenshot" in server_with_screenshot.tools


class TestDispatch:
    def test_initialize_returns_protocol_version(self, server):
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {},
        }))
        assert result["id"] == 1
        assert result["result"]["protocolVersion"] == "2024-11-05"
        assert result["result"]["serverInfo"]["name"] == "clawtouch-mcp"

    def test_tools_list_shape(self, server):
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        }))
        tools = result["result"]["tools"]
        assert len(tools) == 9
        for t in tools:
            assert {"name", "description", "inputSchema"} <= set(t.keys())

    def test_tool_call_click(self, server):
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "hid.click", "arguments": {"x": 500, "y": 300}},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["x"] == 500
        assert payload["y"] == 300

    def test_unknown_method_returns_jsonrpc_error(self, server):
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 99, "method": "nonexistent",
        }))
        assert result["error"]["code"] == -32601

    def test_unknown_tool_returns_error(self, server):
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "hid.nuke_orbit", "arguments": {}},
        }))
        assert result["error"]["code"] == -32000
        assert "unknown tool" in result["error"]["message"]

    def test_notification_returns_none(self, server):
        """Notifications (no `id`) must produce no response."""
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }))
        assert result is None


class TestSafety:
    def test_coords_clamped_to_screen(self, server):
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.click",
                       "arguments": {"x": 99999, "y": -50}},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        # Clamped to screen bounds
        assert payload["x"] == 1919
        assert payload["y"] == 0

    def test_type_length_capped(self, server):
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.type",
                       "arguments": {"text": "x" * (MAX_TYPE_LEN + 1)}},
        }))
        assert result["error"]["code"] == -32000
        assert "too long" in result["error"]["message"]
