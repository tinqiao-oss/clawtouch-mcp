# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Cursor-tracking + absolute-to-relative delta tests.

The firmware is a USB Boot Mouse — it only emits relative deltas.
``clawtouch_mcp.cursor`` provides the host-side OS cursor query and
``ClawTouchMcpServer._absolute_to_relative`` turns an absolute target
into a delta the firmware can execute. These tests cover:

  - the ``CLAWTOUCH_FAKE_CURSOR`` env-var hook (used by conftest.py to
    keep the rest of the suite deterministic on headless CI),
  - the delta math itself,
  - the missing-cursor error path that ``hid.click`` returns when the
    OS query fails (Wayland, unloadable libX11, etc.),
  - the ``relative=true`` fast path that skips the OS query entirely.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from clawtouch_mcp import cursor
from clawtouch_mcp.cursor import (
    _FAKE_CURSOR_ENV,
    availability_hint,
    get_cursor_position,
)
from clawtouch_mcp.server import (
    ClawTouchMcpServer,
    MockBridge,
    ServerConfig,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def server():
    cfg = ServerConfig(screen_w=1920, screen_h=1080, mock=True)
    srv = ClawTouchMcpServer(cfg)
    srv.bridge = MockBridge()
    return srv


class TestFakeCursorEnvHook:
    """The CLAWTOUCH_FAKE_CURSOR env var is the only injection seam
    that survives across `subprocess.Popen`, so the stdio integration
    tests depend on it. Lock its parsing here."""

    def test_well_formed_value_returns_tuple(self, monkeypatch):
        monkeypatch.setenv(_FAKE_CURSOR_ENV, "123,456")
        assert get_cursor_position() == (123, 456)

    def test_whitespace_tolerant(self, monkeypatch):
        monkeypatch.setenv(_FAKE_CURSOR_ENV, "  100 , 200  ")
        assert get_cursor_position() == (100, 200)

    def test_malformed_falls_through_to_real_query(self, monkeypatch):
        # "garbage" can't be parsed as "x,y" — function must NOT crash,
        # it falls through to the real OS query (which on a CI host
        # may also return None, but the important property is "no
        # exception raised").
        monkeypatch.setenv(_FAKE_CURSOR_ENV, "garbage")
        result = get_cursor_position()
        # Either the real OS query succeeded (Windows dev machine) or
        # returned None (headless CI). Both are acceptable; the only
        # failure mode this guards against is an unhandled exception.
        assert result is None or (
            isinstance(result, tuple) and len(result) == 2
        )

    def test_malformed_consults_real_query_not_env(self, monkeypatch):
        # Stronger than the test above: prove a malformed env value actually
        # FALLS THROUGH to the OS query instead of being silently parsed.
        # Pin the platform dispatch to a sentinel so "fell through" is
        # observable regardless of host OS or whether a real cursor exists.
        import clawtouch_mcp.cursor as cur
        monkeypatch.setattr(cur.platform, "system", lambda: "Linux")
        monkeypatch.setattr(cur, "_linux_get_cursor", lambda: (-7, -7))
        monkeypatch.setenv(_FAKE_CURSOR_ENV, "garbage")
        assert get_cursor_position() == (-7, -7)

    def test_env_var_overrides_real_query(self, monkeypatch):
        # Even on a host with a working OS cursor, the env hook wins.
        monkeypatch.setenv(_FAKE_CURSOR_ENV, "7,7")
        assert get_cursor_position() == (7, 7)


class TestAvailabilityHint:
    def test_returns_non_empty_string(self):
        # The hint goes into the error payload that agents see, so it
        # must always be a sensible non-empty string on every platform.
        hint = availability_hint()
        assert isinstance(hint, str)
        assert len(hint) > 0


class TestAbsoluteToRelativeMath:
    def test_target_below_and_right_of_cursor(self, server, monkeypatch):
        monkeypatch.setenv(_FAKE_CURSOR_ENV, "100,200")
        assert server._absolute_to_relative(500, 300) == (400, 100)

    def test_target_above_and_left_of_cursor(self, server, monkeypatch):
        monkeypatch.setenv(_FAKE_CURSOR_ENV, "1000,800")
        assert server._absolute_to_relative(200, 100) == (-800, -700)

    def test_target_at_cursor_yields_zero_delta(self, server, monkeypatch):
        monkeypatch.setenv(_FAKE_CURSOR_ENV, "640,360")
        assert server._absolute_to_relative(640, 360) == (0, 0)

    def test_cursor_unavailable_returns_none(self, server, monkeypatch):
        # Delete the env hook AND make the real query return None too.
        monkeypatch.delenv(_FAKE_CURSOR_ENV, raising=False)
        monkeypatch.setattr(cursor, "_windows_get_cursor", lambda: None)
        monkeypatch.setattr(cursor, "_macos_get_cursor", lambda: None)
        monkeypatch.setattr(cursor, "_linux_get_cursor", lambda: None)
        assert server._absolute_to_relative(500, 300) is None


class TestToolClickAbsolutePath:
    """End-to-end via the dispatcher: agent sends `hid.click(x, y)`
    with default (absolute) semantics, server runs closed-loop
    convergence to land at the target. MockBridge.mouse_move updates
    the cursor state, so under perfect-mock conditions the loop
    short-circuits in one iteration."""

    def test_click_at_500_300_with_cursor_at_960_540(self, server):
        # conftest.py autouse fixture sets FAKE_CURSOR=960,540 (used
        # by MockBridge to seed the dynamic state on first move).
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.click",
                       "arguments": {"x": 500, "y": 300}},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        # _tool_click overlays the mouse_click outcome on top, so ok
        # reflects the click action. Convergence shows up separately.
        assert payload["ok"] is True
        assert payload["x"] == 500
        assert payload["y"] == 300
        assert payload["target_x"] == 500
        assert payload["target_y"] == 300
        assert payload["converged"] is True
        # 1 iter: first delta lands cursor on target, second query
        # short-circuits.
        assert payload["iters"] == 1

    def test_click_clamps_before_computing_delta(self, server):
        # Target (99999, -50) clamps to (1919, 0); MockBridge lands
        # cursor exactly on the clamped target.
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.click",
                       "arguments": {"x": 99999, "y": -50}},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["x"] == 1919
        assert payload["y"] == 0
        assert payload["target_x"] == 1919
        assert payload["target_y"] == 0
        assert payload["converged"] is True


class TestToolClickRelativePath:
    """Passing relative=true must skip the cursor query entirely —
    even when the cursor query would have failed, relative clicks
    should still work."""

    def test_relative_click_skips_cursor_query(self, server, monkeypatch):
        # Force every cursor path to return None — relative=true should
        # not care.
        monkeypatch.delenv(_FAKE_CURSOR_ENV, raising=False)
        monkeypatch.setattr(cursor, "_windows_get_cursor", lambda: None)
        monkeypatch.setattr(cursor, "_macos_get_cursor", lambda: None)
        monkeypatch.setattr(cursor, "_linux_get_cursor", lambda: None)

        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.click",
                       "arguments": {"x": 50, "y": 20, "relative": True}},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["dx"] == 50
        assert payload["dy"] == 20
        assert payload["relative"] is True


class TestToolClickAbsoluteCursorUnavailable:
    """When absolute mode is requested but the OS cursor query fails,
    the tool must return a clear error containing the platform hint
    and the relative=true workaround."""

    def test_returns_error_when_cursor_unavailable(self, server, monkeypatch):
        monkeypatch.delenv(_FAKE_CURSOR_ENV, raising=False)
        monkeypatch.setattr(cursor, "_windows_get_cursor", lambda: None)
        monkeypatch.setattr(cursor, "_macos_get_cursor", lambda: None)
        monkeypatch.setattr(cursor, "_linux_get_cursor", lambda: None)

        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.click",
                       "arguments": {"x": 500, "y": 300}},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        assert "error" in payload
        # The error message must mention the relative=true workaround
        # so the agent knows what to do next.
        assert "relative" in payload["error"].lower()
        # And it must echo the requested target for debugging.
        assert payload["x"] == 500
        assert payload["y"] == 300


class TestToolMoveAndHover:
    """hid.move and hid.hover share the same absolute-by-default
    semantics as hid.click. Quick smoke checks on both."""

    def test_move_absolute_with_fake_cursor(self, server):
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.move",
                       "arguments": {"x": 1000, "y": 500}},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["x"] == 1000
        assert payload["y"] == 500
        assert payload["relative"] is False

    def test_move_relative_skips_cursor(self, server, monkeypatch):
        monkeypatch.delenv(_FAKE_CURSOR_ENV, raising=False)
        monkeypatch.setattr(cursor, "_windows_get_cursor", lambda: None)
        monkeypatch.setattr(cursor, "_macos_get_cursor", lambda: None)
        monkeypatch.setattr(cursor, "_linux_get_cursor", lambda: None)

        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.move",
                       "arguments": {"x": 30, "y": -10, "relative": True}},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["x"] == 30
        assert payload["y"] == -10
        assert payload["relative"] is True

    def test_hover_uses_absolute_path(self, server):
        result = _run(server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.hover",
                       "arguments": {"x": 800, "y": 400, "duration_ms": 1}},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["x"] == 800
        assert payload["y"] == 400
