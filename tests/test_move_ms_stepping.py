"""Path-stepping (``move_ms``) regression tests.

``hid.click`` / ``hid.move`` / ``hid.hover`` accept an optional
``move_ms`` argument. When > 0, the move is broken into ~10 ms
HID reports over that total time so the OS cursor visibly slides
to the target instead of teleporting in a single frame — useful
when recording a demo where a single-frame jump is hard to track.

This is purely linear interpolation: no Bezier, no tremor, no
dwell variance. The intent is the same UX convenience PyAutoGUI
exposes as ``duration=``.

Tests pin:
- ``move_ms`` defaults to 0 (single-shot HID report) — strict
  backward compat with pre-v0.2.8 behavior.
- ``move_ms > 0`` emits N HID reports (4 ≤ N ≤ 100) whose sum of
  per-step deltas equals the requested total move.
- ``move_ms`` works for absolute mode AND ``relative=true`` mode.
- ``hid.hover`` separates ``move_ms`` (path) from ``duration_ms``
  (idle-after-arrival) — they don't conflate.
- ``move_ms`` is clamped to ``MAX_MOVE_MS`` (5000) — agent / typo
  can't lock the handler indefinitely.
"""
from __future__ import annotations

import asyncio

import pytest

from clawtouch_mcp import cursor as _cursor_mod
from clawtouch_mcp.server import (
    MAX_MOVE_MS,
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


def _move_calls(bridge: MockBridge) -> list[tuple[int, int]]:
    """Pull only mouse_move (dx, dy) tuples out of MockBridge log."""
    return [
        (kw["x"], kw["y"])
        for (name, kw) in bridge._calls
        if name == "move"
    ]


# ── default behavior unchanged ──────────────────────────────────

def test_click_no_move_ms_single_shot(server):
    """Default ``move_ms=0`` (omitted) emits exactly ONE mouse_move
    HID report when the converge loop short-circuits on the second
    query (MockBridge lands cursor exactly on target after the first
    delta — no overshoot to correct for)."""
    _cursor_mod._seed_fake_cursor(100, 100)
    _run(server._tool_click(x=500, y=400))
    moves = _move_calls(server.bridge)
    assert len(moves) == 1
    assert moves[0] == (400, 300)


def test_relative_no_move_ms_single_shot(server):
    """Relative mode default also single-shot."""
    _run(server._tool_move(x=120, y=80, relative=True))
    moves = _move_calls(server.bridge)
    assert len(moves) == 1
    assert moves[0] == (120, 80)


# ── move_ms > 0 emits N stepped reports ─────────────────────────

def test_click_with_move_ms_emits_multiple_reports(server):
    """200 ms / 10 ms per step → 20 stepped reports. Sum of step
    deltas must equal requested (400, 300). Under perfect mock the
    slide lands cursor on target, so the post-slide converge stage
    short-circuits and emits no extra reports."""
    _cursor_mod._seed_fake_cursor(100, 100)
    result = _run(server._tool_click(x=500, y=400, move_ms=200))
    moves = _move_calls(server.bridge)
    assert result.get("stepped") is True
    assert result["steps"] == 20
    sum_dx = sum(m[0] for m in moves)
    sum_dy = sum(m[1] for m in moves)
    assert sum_dx == 400
    assert sum_dy == 300
    assert len([c for c in server.bridge._calls if c[0] == "move"]) == 20


def test_relative_move_with_move_ms(server):
    """Relative path stepping works too: chunks agent-supplied delta."""
    result = _run(server._tool_move(x=200, y=150, relative=True, move_ms=100))
    moves = _move_calls(server.bridge)
    assert result.get("stepped") is True
    assert sum(m[0] for m in moves) == 200
    assert sum(m[1] for m in moves) == 150
    # 100ms / 10ms per step = 10 steps
    assert result["steps"] == 10


def test_move_ms_minimum_4_steps(server):
    """``move_ms`` < 40 still gets at least 4 steps so motion is
    visible. ``move_ms=20`` should produce 4 reports, not 2.
    Post-slide converge short-circuits under perfect mock."""
    _cursor_mod._seed_fake_cursor(0, 0)
    result = _run(server._tool_move(x=100, y=100, move_ms=20))
    assert result["steps"] == 4
    moves = _move_calls(server.bridge)
    assert len(moves) == 4
    assert sum(m[0] for m in moves) == 100
    assert sum(m[1] for m in moves) == 100


# ── hover decouples move_ms (path) from duration_ms (idle) ──────

def test_hover_move_ms_and_duration_ms_independent(server):
    """hover's ``duration_ms`` (idle after arrival) and ``move_ms``
    (path stepping) are separate axes. Setting move_ms should NOT
    change the idle behavior."""
    _cursor_mod._seed_fake_cursor(0, 0)
    # move_ms=100 (10 steps) + duration_ms=50 idle (a 50ms sleep)
    result = _run(server._tool_hover(x=200, y=100, move_ms=100, duration_ms=50))
    moves = _move_calls(server.bridge)
    assert len(moves) == 10  # stepped (post-slide converge short-circuits)
    assert result["steps"] == 10
    assert sum(m[0] for m in moves) == 200
    assert sum(m[1] for m in moves) == 100


def test_hover_move_ms_zero_still_idles(server):
    """move_ms=0 (default) → snap mode, then idle for duration_ms."""
    _cursor_mod._seed_fake_cursor(0, 0)
    _run(server._tool_hover(x=200, y=100, duration_ms=50))
    moves = _move_calls(server.bridge)
    assert len(moves) == 1  # snap (converge short-circuits in 1 iter)
    assert moves[0] == (200, 100)


# ── safety cap ──────────────────────────────────────────────────

def test_move_ms_clamped_to_max(server):
    """A huge ``move_ms`` request is silently clamped to MAX_MOVE_MS
    so the handler can't lock for arbitrary duration. 100 step cap
    means even at MAX_MOVE_MS (5 s) we emit at most 100 reports."""
    _cursor_mod._seed_fake_cursor(0, 0)
    # Request 999999 ms; should be clamped to MAX_MOVE_MS and capped
    # at 100 steps.
    result = _run(server._tool_move(x=500, y=500, move_ms=999_999))
    assert result["move_ms"] == MAX_MOVE_MS
    assert result["steps"] == 100  # max step count


# ── edge: zero-distance move with move_ms ───────────────────────

def test_stepped_zero_distance_no_report(server):
    """Stepping a (0, 0) move shouldn't emit any HID reports —
    every per-step delta rounds to 0 and the post-slide converge
    short-circuits because cursor is already on target."""
    _cursor_mod._seed_fake_cursor(500, 500)
    _run(server._tool_move(x=500, y=500, move_ms=100))
    moves = _move_calls(server.bridge)
    assert moves == []
