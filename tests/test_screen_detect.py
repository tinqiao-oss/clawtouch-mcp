# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Screen auto-detect + device.info screen field tests.

Locks down the v0.2.3 auto-detect behavior: when --screen is not given,
server should call _detect_screen() and populate ServerConfig from it,
exposing the result via device.info so an agent can read the true
clamp bounds at runtime instead of guessing.
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import patch


from clawtouch_mcp.server import ClawTouchMcpServer, MockBridge, ServerConfig


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _info(srv):
    result = _run(srv.dispatch({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "device.info", "arguments": {}},
    }))
    return json.loads(result["result"]["content"][0]["text"])


class TestScreenSource:
    def test_explicit_screen_wins_over_detection(self):
        """If --screen WxH was passed, source=explicit and detection is skipped."""
        with patch("clawtouch_mcp.server._detect_screen",
                   return_value=(9999, 9999)) as m:
            cfg = ServerConfig(screen_w=1920, screen_h=1080, mock=True)
            srv = ClawTouchMcpServer(cfg)
        srv.bridge = MockBridge()
        m.assert_not_called()
        assert srv._screen_source == "explicit"
        assert cfg.screen_w == 1920
        assert cfg.screen_h == 1080
        payload = _info(srv)
        assert payload["screen"] == {"width": 1920, "height": 1080,
                                      "source": "explicit"}

    def test_auto_detect_populates_config(self):
        """No --screen + detection succeeds → config populated, source=detected."""
        with patch("clawtouch_mcp.server._detect_screen",
                   return_value=(5120, 1440)):
            cfg = ServerConfig(mock=True)
            srv = ClawTouchMcpServer(cfg)
        srv.bridge = MockBridge()
        assert srv._screen_source == "detected"
        assert cfg.screen_w == 5120
        assert cfg.screen_h == 1440
        payload = _info(srv)
        assert payload["screen"] == {"width": 5120, "height": 1440,
                                      "source": "detected"}

    def test_auto_detect_failure_leaves_unset(self):
        """Detection returns None → config stays None, source=unset, no clamp."""
        with patch("clawtouch_mcp.server._detect_screen", return_value=None):
            cfg = ServerConfig(mock=True)
            srv = ClawTouchMcpServer(cfg)
        srv.bridge = MockBridge()
        assert srv._screen_source == "unset"
        assert cfg.screen_w is None
        assert cfg.screen_h is None
        payload = _info(srv)
        assert payload["screen"] == {"width": None, "height": None,
                                      "source": "unset"}

    def test_partial_explicit_screen_triggers_detect(self):
        """If only width or only height was set, treat as not-explicit."""
        # Should not happen via CLI (--screen WxH parses both or neither)
        # but defensively: half-set screen triggers detection.
        with patch("clawtouch_mcp.server._detect_screen",
                   return_value=(1280, 720)):
            cfg = ServerConfig(screen_w=1920, screen_h=None, mock=True)
            srv = ClawTouchMcpServer(cfg)
        srv.bridge = MockBridge()
        assert srv._screen_source == "detected"
        assert cfg.screen_w == 1280
        assert cfg.screen_h == 720


class TestClampBehaviorAfterAutoDetect:
    def test_clamp_uses_detected_bounds(self):
        """Auto-detected 5120x1440 should clamp click x=99999 → 5119."""
        with patch("clawtouch_mcp.server._detect_screen",
                   return_value=(5120, 1440)):
            cfg = ServerConfig(mock=True)
            srv = ClawTouchMcpServer(cfg)
        srv.bridge = MockBridge()
        result = _run(srv.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.click",
                       "arguments": {"x": 99999, "y": -50}},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        assert payload["x"] == 5119  # clamped to width-1
        assert payload["y"] == 0      # clamped to 0

    def test_no_clamp_when_unset(self):
        """Detection failure → no clamping at all."""
        with patch("clawtouch_mcp.server._detect_screen", return_value=None):
            cfg = ServerConfig(mock=True)
            srv = ClawTouchMcpServer(cfg)
        srv.bridge = MockBridge()
        result = _run(srv.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "hid.click",
                       "arguments": {"x": 99999, "y": 99999}},
        }))
        payload = json.loads(result["result"]["content"][0]["text"])
        # Unclamped: coordinates pass through unchanged.
        assert payload["x"] == 99999
        assert payload["y"] == 99999


class TestRetinaPixelScreenGuard:
    """Warn when an explicit --screen looks like physical Retina pixels on
    macOS (the point-vs-pixel footgun). cursor.get_cursor_position returns
    CoreGraphics POINTS; a pixel-space --screen (e.g. 2880x1800 for a
    1440x900-point display) makes absolute clicks fail to converge. The
    guard fires only on the ~2x-in-both-axes signature, only on darwin,
    only for an explicit screen — never on a wider multi-monitor box.
    """

    def _make(self, w, h):
        # Construct on the real host (the guard no-ops off darwin during
        # __init__); each case invokes the guard explicitly under patched
        # platform + detected-size, mirroring how it runs on a real mac.
        cfg = ServerConfig(screen_w=w, screen_h=h, mock=True)
        srv = ClawTouchMcpServer(cfg)
        srv.bridge = MockBridge()
        return srv

    def _warned(self, srv, detected, platform, caplog):
        caplog.clear()
        with patch("clawtouch_mcp.server.sys.platform", platform), \
             patch("clawtouch_mcp.server._detect_screen", return_value=detected), \
             caplog.at_level(logging.WARNING, logger="clawtouch_mcp.server"):
            srv._warn_if_retina_pixel_screen()
        return any("PHYSICAL Retina pixels" in r.getMessage()
                   for r in caplog.records)

    def test_warns_on_2x_both_axes(self, caplog):
        """2880x1800 vs 1440x900 logical — the canonical Retina trap."""
        srv = self._make(2880, 1800)
        assert self._warned(srv, (1440, 900), "darwin", caplog) is True

    def test_warns_on_3x_both_axes(self, caplog):
        srv = self._make(4320, 2700)
        assert self._warned(srv, (1440, 900), "darwin", caplog) is True

    def test_silent_on_correct_points(self, caplog):
        """--screen already in points (1:1) — nothing to warn about."""
        srv = self._make(1440, 900)
        assert self._warned(srv, (1440, 900), "darwin", caplog) is False

    def test_silent_on_multimonitor_wide(self, caplog):
        """Side-by-side second monitor: width grows, height doesn't → not a
        uniform 2x, so it's a legitimate bounding box, not Retina pixels."""
        srv = self._make(7680, 1440)
        assert self._warned(srv, (3840, 1440), "darwin", caplog) is False

    def test_silent_on_multimonitor_tall(self, caplog):
        srv = self._make(1440, 1800)
        assert self._warned(srv, (1440, 900), "darwin", caplog) is False

    def test_silent_off_darwin(self, caplog):
        """Same 2x signature on Windows — no point-vs-pixel issue there."""
        srv = self._make(2880, 1800)
        assert self._warned(srv, (1440, 900), "win32", caplog) is False

    def test_silent_when_detect_fails(self, caplog):
        """Headless / detection unavailable → can't compare, stay quiet."""
        srv = self._make(2880, 1800)
        assert self._warned(srv, None, "darwin", caplog) is False

    def test_silent_when_screen_not_explicit(self, caplog):
        """Auto-detected size is already point-space-consistent on macOS;
        the guard is gated on source==explicit and must never fire for it."""
        with patch("clawtouch_mcp.server._detect_screen",
                   return_value=(2880, 1800)):
            cfg = ServerConfig(mock=True)
            srv = ClawTouchMcpServer(cfg)
        srv.bridge = MockBridge()
        assert srv._screen_source == "detected"
        assert self._warned(srv, (1440, 900), "darwin", caplog) is False


class TestDetectScreenSmoke:
    def test_returns_tuple_or_none(self):
        """Real call must return Optional[tuple[int,int]] — no exceptions
        leak from the platform-specific paths."""
        from clawtouch_mcp.server import _detect_screen
        result = _detect_screen()
        assert result is None or (
            isinstance(result, tuple)
            and len(result) == 2
            and all(isinstance(n, int) and n > 0 for n in result)
        )
