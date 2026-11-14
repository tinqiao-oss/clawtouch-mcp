# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Beijing Tinqiao Technology Co., Ltd.
"""Absolute-pointer hop (firmware 1.2+, MOUSE_MOVE_ABS 0x15).

A snap move may start with ONE absolute report and let the existing
converge loop finish. What is pinned here:

  - the bridge maps ACK / ERROR(UNKNOWN_COMMAND) / anything else to
    "ok" / "unsupported" / "failed", and stops asking after the firmware
    said it does not know the opcode — until the next connect;
  - the server converts a pixel on the primary monitor to 1..32767 the way
    Windows was measured to map it, and never sends (0, 0), which Windows
    drops;
  - the hop is skipped off Windows, off the primary monitor, under
    --no-abs-pointer, and in --mock;
  - a hop that lands where the mapping did not predict switches hops off
    for good, and the converge loop still delivers the move;
  - the converge loop always runs after a hop, so a hop can never be what
    vouches for a move.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from clawtouch_mcp import cursor as _cursor_mod
from clawtouch_mcp import server as server_mod
from clawtouch_mcp.bridge import SerialHidBridge
from clawtouch_mcp.protocol import ABS_POINTER_MAX, CommandType, ErrorCode
from clawtouch_mcp.server import (
    ABS_HOP_TRUST_PX,
    ClawTouchMcpServer,
    MockBridge,
    ServerConfig,
)

PRIMARY = (5120, 1440)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── bridge ──────────────────────────────────────────────────────────


def _bridge_answering(*responses):
    bridge = SerialHidBridge("/dev/null", baudrate=115200)
    sent = []
    queue = list(responses)

    async def fake_send_raw(cmd):
        sent.append(cmd)
        return queue.pop(0)

    bridge._send_raw = fake_send_raw
    return bridge, sent


def _ack():
    return SimpleNamespace(cmd_type=CommandType.ACK, payload=b"")


def _err(code, tail=b""):
    return SimpleNamespace(cmd_type=CommandType.ERROR, payload=bytes([code]) + tail)


def test_bridge_ok_sends_move_abs_frame():
    bridge, sent = _bridge_answering(_ack())
    assert asyncio.run(bridge.mouse_move_absolute(16387, 9)) == "ok"
    assert sent[0].cmd_type == CommandType.MOUSE_MOVE_ABS
    assert sent[0].payload == bytes([0x03, 0x40, 0x09, 0x00])


def test_bridge_unknown_command_is_unsupported_and_remembered():
    bridge, sent = _bridge_answering(_err(ErrorCode.UNKNOWN_COMMAND, b"0x15"))
    assert asyncio.run(bridge.mouse_move_absolute(1, 1)) == "unsupported"
    # No second frame: the answer is remembered for this connection.
    assert asyncio.run(bridge.mouse_move_absolute(1, 1)) == "unsupported"
    assert len(sent) == 1
    # An older firmware answering as documented is not a device fault.
    assert bridge._last_error_detail is None


def test_bridge_forgets_unsupported_on_connect():
    """A replug can bring different firmware, so connect() re-arms the
    probe. connect() itself is not run (it would open a port); the reset
    it performs is."""
    bridge, _ = _bridge_answering(_err(ErrorCode.UNKNOWN_COMMAND))
    asyncio.run(bridge.mouse_move_absolute(1, 1))
    assert bridge._abs_unsupported is True
    import inspect
    assert "self._abs_unsupported = False" in inspect.getsource(SerialHidBridge.connect)


@pytest.mark.parametrize("resp", [None, _err(ErrorCode.INVALID_PAYLOAD)])
def test_bridge_other_answers_are_failed_not_unsupported(resp):
    bridge, sent = _bridge_answering(resp, _ack())
    assert asyncio.run(bridge.mouse_move_absolute(5, 5)) == "failed"
    # Not remembered: a timeout says nothing about the firmware.
    assert asyncio.run(bridge.mouse_move_absolute(5, 5)) == "ok"
    assert len(sent) == 2


# ── server ──────────────────────────────────────────────────────────


class _AbsBridge(MockBridge):
    """MockBridge whose firmware has an absolute pointer. ``land`` maps a
    sent (nx, ny) to where the OS puts the cursor; the default is the
    Windows mapping measured on hardware (primary monitor, floor)."""

    def __init__(self, land=None, status="ok"):
        super().__init__()
        self.status = status
        self.land = land or (lambda nx, ny: (
            nx * PRIMARY[0] // (ABS_POINTER_MAX + 1),
            ny * PRIMARY[1] // (ABS_POINTER_MAX + 1)))

    async def mouse_move_absolute(self, x, y):
        self._calls.append(("move_abs", {"x": x, "y": y}))
        if self.status == "ok":
            _cursor_mod._update_fake_cursor(*self.land(x, y), relative=False)
        return self.status


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(server_mod, "_detect_screen", lambda: PRIMARY)
    _cursor_mod._seed_fake_cursor(3782, 1053)


def _server(bridge, **cfg):
    srv = ClawTouchMcpServer(ServerConfig(screen_w=7680, screen_h=1440, mock=True, **cfg))
    srv.bridge = bridge
    return srv


def _calls(bridge, kind):
    return [c for c in bridge._calls if c[0] == kind]


@pytest.mark.parametrize("tx,ty", [(0, 0), (2560, 720), (5119, 1439), (1, 1), (4097, 3)])
def test_hop_lands_on_the_pixel_and_converge_confirms(win, tx, ty):
    bridge = _AbsBridge()
    srv = _server(bridge)
    result = _run(srv._move_to_absolute(tx, ty))
    assert result["abs_hop"] is True
    assert result["converged"] is True and result["iters"] == 0
    assert (result["x"], result["y"]) == (tx, ty)
    assert _calls(bridge, "move") == []  # no relative leg was needed
    (_, sent), = _calls(bridge, "move_abs")
    assert 1 <= sent["x"] <= ABS_POINTER_MAX and 1 <= sent["y"] <= ABS_POINTER_MAX


def test_hop_never_sends_zero_zero(win):
    """Windows drops an absolute report of exactly (0, 0) (measured)."""
    bridge = _AbsBridge()
    _run(_server(bridge)._move_to_absolute(0, 0))
    (_, sent), = _calls(bridge, "move_abs")
    assert (sent["x"], sent["y"]) != (0, 0)


def test_hop_skipped_off_the_primary_monitor(win):
    """Windows maps the absolute pointer to the primary monitor only; a
    point on the secondary goes the relative way."""
    bridge = _AbsBridge()
    result = _run(_server(bridge)._move_to_absolute(6000, 700))
    assert "abs_hop" not in result
    assert _calls(bridge, "move_abs") == []
    assert result["converged"] is True


def test_hop_skipped_off_windows(win, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    bridge = _AbsBridge()
    result = _run(_server(bridge)._move_to_absolute(100, 100))
    assert _calls(bridge, "move_abs") == [] and "abs_hop" not in result


def test_hop_skipped_with_no_abs_pointer_flag(win):
    bridge = _AbsBridge()
    result = _run(_server(bridge, abs_pointer=False)._move_to_absolute(100, 100))
    assert _calls(bridge, "move_abs") == [] and "abs_hop" not in result


def test_mock_bridge_stays_on_the_relative_path(win):
    bridge = MockBridge()
    result = _run(_server(bridge)._move_to_absolute(100, 100))
    assert "abs_hop" not in result
    assert _calls(bridge, "move")  # relative moves did the work


@pytest.mark.parametrize("status", ["unsupported", "failed"])
def test_hop_that_does_not_ack_falls_back_to_relative(win, status):
    bridge = _AbsBridge(status=status)
    result = _run(_server(bridge)._move_to_absolute(400, 300))
    assert "abs_hop" not in result
    assert result["converged"] is True
    assert _calls(bridge, "move")


def test_hop_that_misses_switches_hops_off_and_the_move_still_lands(win):
    """A mapping other than the measured one (here: the whole 7680-wide
    virtual desktop) must not be trusted again, and the converge loop must
    still deliver the cursor."""
    virtual = lambda nx, ny: (nx * 7680 // (ABS_POINTER_MAX + 1),  # noqa: E731
                              ny * 1440 // (ABS_POINTER_MAX + 1))
    bridge = _AbsBridge(land=virtual)
    srv = _server(bridge)
    first = _run(srv._move_to_absolute(2000, 700))
    assert "abs_hop" not in first
    assert first["converged"] is True and (first["x"], first["y"]) == (2000, 700)
    assert srv._abs_hop_off and "px off" in srv._abs_hop_off
    _run(srv._move_to_absolute(100, 100))
    assert len(_calls(bridge, "move_abs")) == 1  # never tried again


def test_hop_within_trust_is_kept(win):
    off = ABS_HOP_TRUST_PX  # exactly at the limit still counts as landed
    bridge = _AbsBridge(land=lambda nx, ny: (500 + off, 400))
    srv = _server(bridge)
    result = _run(srv._move_to_absolute(500, 400))
    assert result["abs_hop"] is True and srv._abs_hop_off is None
    assert result["converged"] is True and (result["x"], result["y"]) == (500, 400)


# ── held buttons, and the one width with no exact mapping ──────────


def test_no_hop_while_a_button_is_held(win):
    """A drag's second leg keeps the relative path: it is the only one
    measured with a button down."""
    bridge = _AbsBridge()
    srv = _server(bridge)
    _run(srv._press("left"))
    result = _run(srv._move_to_absolute(400, 300))
    assert "abs_hop" not in result and _calls(bridge, "move_abs") == []
    _run(srv._release("left"))
    assert _run(srv._move_to_absolute(500, 300))["abs_hop"] is True


def test_drag_hops_to_the_start_but_not_while_dragging(win):
    bridge = _AbsBridge()
    srv = _server(bridge)
    out = _run(srv._tool_drag(from_x=100, from_y=100, to_x=900, to_y=500))
    assert out["ok"] is True
    hops = _calls(bridge, "move_abs")
    assert len(hops) == 1  # the start point only
    order = [c[0] for c in bridge._calls]
    assert order.index("move_abs") < order.index("button_down")
    assert srv._buttons_held == set()


def test_failed_release_keeps_hops_off_until_release_all(win):
    bridge = _AbsBridge()
    srv = _server(bridge)

    async def failing_up(button="left"):
        return False

    _run(srv._press("left"))
    bridge.mouse_button_up = failing_up
    _run(srv._release("left"))
    assert _run(srv._move_to_absolute(400, 300)).get("abs_hop") is None
    _run(srv._release_everything())
    assert _run(srv._move_to_absolute(450, 300))["abs_hop"] is True


def test_panic_key_release_clears_held_buttons(win):
    bridge = _AbsBridge()
    srv = _server(bridge)
    _run(srv._press("left"))
    _run(srv._tool_key_release())
    assert srv._buttons_held == set()


def test_no_hop_on_a_32768_wide_primary(win, monkeypatch):
    """One unit per pixel: the forced minimum of 1 would land on pixel 1."""
    monkeypatch.setattr(server_mod, "_detect_screen", lambda: (32768, 1440))
    bridge = _AbsBridge()
    result = _run(_server(bridge)._move_to_absolute(0, 10))
    assert _calls(bridge, "move_abs") == [] and "abs_hop" not in result


def test_button_names_are_matched_case_insensitively(win):
    bridge = _AbsBridge()
    srv = _server(bridge)
    _run(srv._press("LEFT"))
    _run(srv._release("left"))
    assert srv._buttons_held == set()


def test_empty_key_release_with_modifiers_is_still_the_panic_stop(win):
    """bridge.key_release sends KEY_RELEASE(0, 0) for any empty key."""
    bridge = _AbsBridge()
    srv = _server(bridge)
    _run(srv._press("left"))
    _run(srv._tool_key_release(modifiers=["shift"]))
    assert srv._buttons_held == set()


def test_a_click_releases_the_button_it_clicks(win):
    """MOUSE_CLICK presses and releases, so a button held before it is up."""
    bridge = _AbsBridge()
    srv = _server(bridge)
    _run(srv._press("left"))
    _run(srv._tool_batch(ops=[{"type": "click", "x": 300, "y": 300}]))
    assert srv._buttons_held == set()
    assert _run(srv._move_to_absolute(600, 300))["abs_hop"] is True


def test_unknown_button_name_is_not_recorded(win):
    bridge = _AbsBridge()
    srv = _server(bridge)
    _run(srv._press("thumb"))  # MockBridge accepts anything; the real bridge raises
    assert srv._buttons_held == set()
