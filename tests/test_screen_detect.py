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
from unittest.mock import patch

import pytest

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
