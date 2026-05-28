# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Closed-loop convergence regression tests.

macOS pointer ballistics non-linearly scales single HID deltas
(~110% in low-speed segment, measured on Ventura ARM64). The
server's ``_converge_to_target`` helper must iterate until the
residual falls within MOVE_TOLERANCE or MOVE_MAX_ITERS is hit,
returning the actual landing position (not the requested target)
when convergence fails. This file pins:

  - already-at-target / within-tolerance short-circuits with
    ``iters=0`` and no mouse_move report,
  - simulated 110% amplification converges within MOVE_MAX_ITERS,
  - a stuck cursor (mock that never reflects deltas) bails after
    MOVE_MAX_ITERS with ``ok=False`` / ``converged=False`` and
    returns the stuck actual position + ``residual_*`` fields,
  - glide mode (``move_ms>0``) runs the post-slide converge stage
    to clean up macOS-style overshoot at the slide's final step.
"""
from __future__ import annotations

import asyncio

import pytest

from clawtouch_mcp import cursor as _cursor_mod
from clawtouch_mcp.server import (
    MOVE_MAX_ITERS,
    MOVE_TOLERANCE,
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


def _install_overshoot_bridge(server, *, accel: float, start: tuple[int, int]):
    """Replace the server's MockBridge.mouse_move with one that
    simulates pointer-ballistics amplification: the firmware emits
    ``dx`` but the cursor actually moves ``dx * accel``."""
    _cursor_mod._seed_fake_cursor(*start)

    async def amplified(x, y, *, relative=False):
        server.bridge._calls.append(("move", {"x": x, "y": y, "relative": relative}))
        scaled_x = round(x * accel)
        scaled_y = round(y * accel)
        _cursor_mod._update_fake_cursor(scaled_x, scaled_y, relative=relative)
        return True

    server.bridge.mouse_move = amplified


# ── 1. short-circuit when already on / within tolerance of target ──

def test_already_at_target_short_circuits_with_zero_iters(server):
    _cursor_mod._seed_fake_cursor(500, 400)
    result = _run(server._move_to_absolute(500, 400))
    assert result["converged"] is True
    assert result["iters"] == 0
    assert result["x"] == 500
    assert result["y"] == 400
    assert result["target_x"] == 500
    assert result["target_y"] == 400
    moves = [c for c in server.bridge._calls if c[0] == "move"]
    assert moves == []


def test_within_tolerance_short_circuits(server):
    # Start 2 px off target — within MOVE_TOLERANCE (3) so the loop
    # should treat it as converged without emitting any HID report.
    _cursor_mod._seed_fake_cursor(500 - 2, 400 + 2)
    result = _run(server._move_to_absolute(500, 400))
    assert result["converged"] is True
    assert result["iters"] == 0
    moves = [c for c in server.bridge._calls if c[0] == "move"]
    assert moves == []


# ── 2. simulated macOS overshoot converges in ≤ MOVE_MAX_ITERS ─────

def test_macos_overshoot_converges_within_max_iters(server):
    _install_overshoot_bridge(server, accel=1.1, start=(0, 0))
    result = _run(server._move_to_absolute(500, 400))
    assert result["converged"] is True, result
    assert 0 < result["iters"] <= MOVE_MAX_ITERS
    assert abs(result["x"] - 500) <= MOVE_TOLERANCE
    assert abs(result["y"] - 400) <= MOVE_TOLERANCE
    assert result["target_x"] == 500
    assert result["target_y"] == 400


# ── 3. stuck cursor bails after MOVE_MAX_ITERS, returns actual ─────

def test_cursor_stuck_bails_at_max_iters_with_actual_position(server):
    """When mouse_move never moves the cursor (mock that drops the
    delta), converge must bail after MOVE_MAX_ITERS with
    ``converged=False`` / ``ok=False``, returning the stuck actual
    position rather than the target — agent inspects residual to
    decide whether to retry."""
    _cursor_mod._seed_fake_cursor(100, 100)

    async def noop_move(x, y, *, relative=False):
        server.bridge._calls.append(("move", {"x": x, "y": y, "relative": relative}))
        # Intentionally do NOT touch the dynamic cursor state.
        return True

    server.bridge.mouse_move = noop_move
    result = _run(server._move_to_absolute(800, 600))
    assert result["converged"] is False
    assert result["ok"] is False
    assert result["iters"] == MOVE_MAX_ITERS
    assert result["x"] == 100
    assert result["y"] == 100
    assert result["target_x"] == 800
    assert result["target_y"] == 600
    assert result["residual_x"] == 700
    assert result["residual_y"] == 500
    assert "hint" in result
    moves = [c for c in server.bridge._calls if c[0] == "move"]
    assert len(moves) == MOVE_MAX_ITERS


# ── 4. glide mode + macOS amplification: post-slide converge ──────

def test_stepped_mode_converges_after_slide(server):
    """Glide mode under simulated 110% amplification: the slide
    itself overshoots, then the post-slide converge stage pulls the
    cursor onto target."""
    _install_overshoot_bridge(server, accel=1.1, start=(0, 0))
    result = _run(server._stepped_move_to_absolute(500, 400, move_ms=100))
    assert result["converged"] is True, result
    assert result["stepped"] is True
    assert abs(result["x"] - 500) <= MOVE_TOLERANCE
    assert abs(result["y"] - 400) <= MOVE_TOLERANCE


def test_stepped_mode_converge_uses_one_fewer_iter_budget(server):
    """Glide-mode post-slide converge gets MOVE_MAX_ITERS - 1
    (= 3) iterations, not MOVE_MAX_ITERS — the slide already
    landed within tens of pixels so 3 settles is sufficient. Use
    a noop bridge for the converge stage so we can count moves
    emitted strictly inside the converge loop."""
    _cursor_mod._seed_fake_cursor(0, 0)
    # Disable the bridge AFTER the slide by patching it during the
    # converge phase: easier approach is to count moves before/after.
    # Use noop from the start so slide also emits 0 reflux, then
    # confirm converge emits exactly MOVE_MAX_ITERS - 1 attempts.

    async def noop_move(x, y, *, relative=False):
        server.bridge._calls.append(("move", {"x": x, "y": y, "relative": relative}))
        return True

    server.bridge.mouse_move = noop_move
    result = _run(server._stepped_move_to_absolute(500, 400, move_ms=100))
    assert result["converged"] is False
    assert result["iters"] == MOVE_MAX_ITERS - 1
