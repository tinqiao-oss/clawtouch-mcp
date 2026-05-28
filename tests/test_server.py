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
    # Each call gets a fresh loop so tests can't leak state to each
    # other. Close it in `finally` to avoid the ResourceWarning that
    # used to bubble up on Windows + orphan any idle-watch tasks the
    # server scheduled during the test.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestToolRegistry:
    def test_baseline_15_tools(self, server):
        # 9 v1.0 baseline + 6 v1.1 additions (mouse_button_down/up, drag,
        # key_press/release, hold_key). hid.screenshot is opt-in via
        # --allow-screenshot and tested separately.
        names = set(server.tools.keys())
        expected = {
            # v1.0
            "hid.click", "hid.move", "hid.hover", "hid.type",
            "hid.scroll", "hid.key", "hid.release_all",
            "device.list", "device.info",
            # v1.1 — independent primitives + composed gestures
            "hid.mouse_button_down", "hid.mouse_button_up", "hid.drag",
            "hid.key_press", "hid.key_release", "hid.hold_key",
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
        # 9 v1.0 + 6 v1.1 = 15 (screenshot excluded — opt-in)
        assert len(tools) == 15
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

    def test_unknown_tool_returns_iserror_content(self, server):
        # MCP spec compliance: tool-level errors (including "unknown
        # tool") must come back as `result.content + isError:true`,
        # NOT as a JSON-RPC error. Compliant clients (Claude Desktop,
        # Cline) show the message to the agent so it can react.
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "hid.nuke_orbit", "arguments": {}},
        }))
        assert "error" not in result, "should be result.isError, not JSON-RPC error"
        assert result["result"]["isError"] is True
        assert "unknown tool" in result["result"]["content"][0]["text"]

    def test_notification_returns_none(self, server):
        """Notifications (no `id`) must produce no response."""
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }))
        assert result is None


class TestKeyShortcut:
    """`hid.key` accepts both structured `{key, modifiers}` and shorthand
    like `"ctrl+c"` / `"ctrl+alt+l"` in the `key` field. The handler
    splits the shorthand prefix into modifiers before calling the bridge.
    """

    def _last_key_call(self, server):
        return [c for c in server.bridge._calls if c[0] == "key"][-1][1]

    def test_structured_form_unchanged(self, server):
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.key",
                       "arguments": {"key": "c", "modifiers": ["ctrl"]}},
        }))
        assert json.loads(result["result"]["content"][0]["text"])["ok"] is True
        call = self._last_key_call(server)
        assert call == {"modifiers": ["ctrl"], "key": "c"}

    def test_shorthand_single_modifier(self, server):
        _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.key", "arguments": {"key": "ctrl+c"}},
        }))
        assert self._last_key_call(server) == {"modifiers": ["ctrl"], "key": "c"}

    def test_shorthand_multiple_modifiers(self, server):
        _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.key", "arguments": {"key": "ctrl+alt+l"}},
        }))
        assert self._last_key_call(server) == {
            "modifiers": ["ctrl", "alt"], "key": "l",
        }

    def test_shorthand_merges_with_explicit_modifiers_no_dupes(self, server):
        _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.key",
                       "arguments": {"key": "ctrl+c", "modifiers": ["ctrl", "shift"]}},
        }))
        # explicit + shorthand combined, dedup, "ctrl" kept once
        call = self._last_key_call(server)
        assert call["key"] == "c"
        assert sorted(call["modifiers"]) == ["ctrl", "shift"]

    def test_named_key_with_plus_in_modifier_prefix(self, server):
        _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.key", "arguments": {"key": "shift+enter"}},
        }))
        assert self._last_key_call(server) == {
            "modifiers": ["shift"], "key": "enter",
        }

    def test_literal_plus_is_not_split(self, server):
        """If the prefix isn't all modifiers, treat the whole string as
        the key name (firmware will reject if unknown, but we don't
        eat the '+')."""
        _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.key", "arguments": {"key": "foo+bar"}},
        }))
        assert self._last_key_call(server) == {"modifiers": [], "key": "foo+bar"}

    def test_case_insensitive_modifier_prefix(self, server):
        _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.key", "arguments": {"key": "CTRL+ALT+L"}},
        }))
        assert self._last_key_call(server) == {
            "modifiers": ["ctrl", "alt"], "key": "L",
        }


class TestV11DragAndHold:
    """v1.1 additions — hid.drag / hid.hold_key compose primitives;
    hid.mouse_button_down/up / hid.key_press/release are direct
    bridge exposures. Verify the composed paths emit the expected
    sub-call sequence on MockBridge."""

    def _calls(self, server, name=None):
        if name is None:
            return list(server.bridge._calls)
        return [c for c in server.bridge._calls if c[0] == name]

    def test_mouse_button_down_up_directly(self, server):
        _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.mouse_button_down",
                       "arguments": {"button": "right"}},
        }))
        _run(server.dispatch({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "hid.mouse_button_up",
                       "arguments": {"button": "right"}},
        }))
        downs = self._calls(server, "button_down")
        ups = self._calls(server, "button_up")
        assert downs and downs[-1] == ("button_down", {"button": "right"})
        assert ups and ups[-1] == ("button_up", {"button": "right"})

    def test_drag_emits_press_move_release_sequence(self, server):
        # snap-mode drag (move_ms=0) — exactly: move-to-src → button_down
        # → move-to-dst → button_up. Glide mode is tested separately.
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.drag", "arguments": {
                "from_x": 100, "from_y": 100,
                "to_x": 500, "to_y": 400,
                "button": "left", "move_ms": 0,
            }},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["ok"] is True
        # Pull mouse-related call sequence
        sequence = [c[0] for c in server.bridge._calls
                    if c[0] in ("move", "button_down", "button_up")]
        # Drag must press BEFORE the destination move and release AFTER it.
        assert sequence.count("button_down") == 1
        assert sequence.count("button_up") == 1
        down_idx = sequence.index("button_down")
        up_idx = sequence.index("button_up")
        assert up_idx > down_idx, "button_up must follow button_down"
        # At least one move sits BETWEEN press and release (the drag move).
        moves_between = sequence[down_idx + 1:up_idx]
        assert "move" in moves_between, (
            "drag must move while the button is held; got: %s" % sequence
        )

    def test_drag_releases_on_mid_drag_exception(self, server, monkeypatch):
        """If the glided destination move raises, button must still release —
        leaving a button stuck down is worse than the partial drag."""
        calls_after_failure: list[str] = []

        original_move = server.bridge.mouse_move
        async def failing_move(x, y, *, relative=False):  # noqa: ANN001
            # First call (move-to-source) succeeds. Subsequent move (the
            # destination move under hold) raises.
            cnt = len([c for c in server.bridge._calls if c[0] == "move"])
            if cnt >= 1:
                calls_after_failure.append("raised")
                raise RuntimeError("simulated mid-drag failure")
            return await original_move(x, y, relative=relative)

        monkeypatch.setattr(server.bridge, "mouse_move", failing_move)

        try:
            _run(server.dispatch({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "hid.drag", "arguments": {
                    "from_x": 100, "from_y": 100,
                    "to_x": 500, "to_y": 400,
                    "move_ms": 0,
                }},
            }))
        except RuntimeError:
            pass  # expected — but the release must have run via finally

        # Button release must have happened even though move raised.
        ups = self._calls(server, "button_up")
        assert ups, "mid-drag exception must still trigger button_up"

    def test_key_press_release_directly(self, server):
        _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.key_press",
                       "arguments": {"key": "shift"}},
        }))
        _run(server.dispatch({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "hid.key_release",
                       "arguments": {"key": "shift"}},
        }))
        presses = self._calls(server, "key_press")
        releases = self._calls(server, "key_release")
        assert presses and presses[-1][1]["key"] == "shift"
        assert releases and releases[-1][1]["key"] == "shift"

    def test_key_release_no_args_is_release_all(self, server):
        _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.key_release", "arguments": {}},
        }))
        releases = self._calls(server, "key_release")
        assert releases and releases[-1][1]["key"] == ""

    def test_hold_key_emits_press_then_release(self, server):
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.hold_key", "arguments": {
                "key": "a", "duration_ms": 5,
            }},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["duration_ms"] == 5
        # Press came before release
        sequence = [c[0] for c in server.bridge._calls
                    if c[0] in ("key_press", "key_release")]
        assert sequence == ["key_press", "key_release"]


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
        # MCP spec compliance: ValueError from a tool handler comes
        # back as isError content, NOT JSON-RPC error.
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.type",
                       "arguments": {"text": "x" * (MAX_TYPE_LEN + 1)}},
        }))
        assert "error" not in result
        assert result["result"]["isError"] is True
        assert "too long" in result["result"]["content"][0]["text"]
