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
    MAX_CONSECUTIVE_MOVE_TIMEOUTS,
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
    # Start 2 px off target — within MOVE_TOLERANCE (5) so the loop
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


def test_stepped_mode_converge_uses_full_iter_budget(server):
    """Glide-mode post-slide converge gets the FULL MOVE_MAX_ITERS
    budget (same as snap), NOT one fewer. The slide's final micro-step
    is itself ballistics-amplified, so it lands tens of px off — the
    same order as a cold-start move — and earns no smaller budget.
    (Regression: an earlier ``MOVE_MAX_ITERS - 1`` left glide landings
    4-7 px off that the click gate then refused; real-hardware mac
    dogfood 2026-06-04.) Use a noop bridge so the converge stage never
    lands, and confirm it bails after exactly MOVE_MAX_ITERS."""
    _cursor_mod._seed_fake_cursor(0, 0)

    async def noop_move(x, y, *, relative=False):
        server.bridge._calls.append(("move", {"x": x, "y": y, "relative": relative}))
        return True

    server.bridge.mouse_move = noop_move
    result = _run(server._stepped_move_to_absolute(500, 400, move_ms=100))
    assert result["converged"] is False
    assert result["iters"] == MOVE_MAX_ITERS


# ── 5. dead-device death-spiral guard (no full-budget grind) ──────

def test_dead_device_bails_early_on_consecutive_ack_timeouts(server):
    """A device that never ACKs (unplugged / firmware hung) must NOT grind
    through the full MOVE_MAX_ITERS at the per-ACK timeout each. converge
    bails after MAX_CONSECUTIVE_MOVE_TIMEOUTS un-ACKed reports, flags
    device_nonresponsive, and reports the (unmoved) actual position. This is
    the fix for the ~110 s single-move hang on a dead device."""
    # Sanity: the guard must actually be tighter than the full budget, else
    # this test would pass trivially.
    assert MAX_CONSECUTIVE_MOVE_TIMEOUTS < MOVE_MAX_ITERS
    _cursor_mod._seed_fake_cursor(100, 100)

    async def dead_move(x, y, *, relative=False):
        server.bridge._calls.append(("move", {"x": x, "y": y, "relative": relative}))
        return False  # firmware never ACKs

    server.bridge.mouse_move = dead_move
    result = _run(server._move_to_absolute(800, 600))
    assert result["ok"] is False
    assert result["converged"] is False
    assert result["device_nonresponsive"] is True
    assert result["iters"] == MAX_CONSECUTIVE_MOVE_TIMEOUTS
    assert result["x"] == 100 and result["y"] == 100   # never moved
    moves = [c for c in server.bridge._calls if c[0] == "move"]
    assert len(moves) == MAX_CONSECUTIVE_MOVE_TIMEOUTS  # not MOVE_MAX_ITERS


def test_dead_device_aborts_glide_without_running_converge(server):
    """Glide mode on a dead device aborts the slide after the consecutive-
    timeout threshold and does NOT run the post-slide converge stage (which
    would only re-spend the same ACK-timeout dead-air). ok:False so a
    dependent click won't fire."""
    _cursor_mod._seed_fake_cursor(0, 0)

    async def dead_move(x, y, *, relative=False):
        server.bridge._calls.append(("move", {"x": x, "y": y, "relative": relative}))
        return False

    server.bridge.mouse_move = dead_move
    result = _run(server._stepped_move_to_absolute(500, 400, move_ms=100))
    assert result["ok"] is False
    assert result["converged"] is False
    assert result["device_nonresponsive"] is True
    assert result["slide_acked"] is False
    moves = [c for c in server.bridge._calls if c[0] == "move"]
    # Bailed during the slide; far fewer than the steps + MOVE_MAX_ITERS the
    # old death-spiral would have emitted.
    assert len(moves) == MAX_CONSECUTIVE_MOVE_TIMEOUTS


def test_single_transient_drop_does_not_trip_the_guard(server):
    """A lone non-ACK on an otherwise-live device must NOT abort the move —
    the counter resets on the next ACK, so the closed loop still converges."""
    _cursor_mod._seed_fake_cursor(100, 100)
    state = {"first": True}

    async def flaky_move(x, y, *, relative=False):
        server.bridge._calls.append(("move", {"x": x, "y": y, "relative": relative}))
        if state["first"]:
            state["first"] = False
            return False  # one transient drop, but cursor still moves
        _cursor_mod._update_fake_cursor(x, y, relative=relative)
        return True

    server.bridge.mouse_move = flaky_move
    result = _run(server._move_to_absolute(500, 400))
    assert result["converged"] is True       # rode through the single drop
    assert "device_nonresponsive" not in result


def test_stepped_mode_converges_under_strong_amplification(server):
    """Regression (real-hardware mac dogfood 2026-06-04): under stronger
    ballistics amplification the glide's post-slide residual needs MORE
    than the old 3-pass budget to settle. With the full MOVE_MAX_ITERS
    budget (and the looser MOVE_TOLERANCE) the move now converges and the
    click gate is no longer tripped on a 4-7 px near-miss. The same
    scenario at the old (3-iter / 3-px) calibration left converged=False.

    The original assertion here was ``iters > 3`` — a proxy for "the glide
    got the full budget" that only held while every pass shed a fixed ~30%
    of the residual. The loop now divides each command by a gain measured
    from the previous pass, so this converges in a couple of passes
    instead of five; pinning the old pass count would be pinning the old
    inefficiency. What must stay true is the budget, so that is asserted
    directly."""
    from clawtouch_mcp.server import MOVE_MAX_ITERS as _BUDGET
    _install_overshoot_bridge(server, accel=1.3, start=(0, 0))
    result = _run(server._stepped_move_to_absolute(1200, 800, move_ms=130))
    assert result["converged"] is True, result
    assert 0 < result["iters"] <= _BUDGET, result
    assert abs(result["x"] - 1200) <= MOVE_TOLERANCE
    assert abs(result["y"] - 800) <= MOVE_TOLERANCE


def test_strong_windows_style_amplification_converges(server):
    """Windows with "Enhanced pointer precision" (the shipped default)
    amplifies a large HID delta ~2.5x — measured on real hardware, not
    assumed: commanding 127 px moved the cursor 319, commanding -1000
    moved -2516.

    A loop that commands the raw residual cannot converge at that gain: it
    overshoots by 150% every pass and oscillates until the budget runs
    out, which is exactly what a long click across a wide desktop did.
    Dividing by a gain learned from the previous pass fixes it without any
    per-platform ballistics table."""
    _install_overshoot_bridge(server, accel=2.5, start=(0, 0))
    result = _run(server._move_to_absolute(1800, 1000))
    assert result["converged"] is True, result
    assert abs(result["x"] - 1800) <= MOVE_TOLERANCE, result
    assert abs(result["y"] - 1000) <= MOVE_TOLERANCE, result


def test_a_damping_host_also_converges(server):
    """The estimator must work in both directions — a host that moves the
    cursor LESS than commanded (pointer speed turned down) would stall a
    loop that only ever divided by a number bigger than one."""
    _install_overshoot_bridge(server, accel=0.4, start=(0, 0))
    result = _run(server._move_to_absolute(1500, 800))
    assert result["converged"] is True, result
    assert abs(result["x"] - 1500) <= MOVE_TOLERANCE, result


def test_gain_estimate_ignores_an_edge_clipped_pass(server):
    """A move clipped by the screen edge travelled less than the OS would
    have moved it. Folding that into the estimate would teach the loop the
    host damps input and make it overshoot harder on the next pass — the
    opposite of the truth."""
    server.config.screen_w = 1920
    server.config.screen_h = 1080
    gain, residual = server._update_gain(
        2.5,
        # commanded +400 in x, but the cursor stopped dead on the right edge
        (400, 0, (1600, 500)),
        (1919, 500), 1000, 500, None,
    )
    assert gain == 2.5, "an edge-clipped axis must not move the estimate"
    assert residual == 919


def test_pointer_gain_survives_between_moves(server):
    """The gain describes the HOST's mouse settings, not one move. Relearning
    it from scratch every time costs a wasted overshoot per click — visible
    as the cursor flying past the target and coming back."""
    _install_overshoot_bridge(server, accel=2.5, start=(0, 0))
    first = _run(server._move_to_absolute(1500, 900))
    assert first["converged"] is True, first
    learned = server._pointer_gain
    assert 2.0 < learned < 3.0, learned

    _cursor_mod._seed_fake_cursor(0, 0)
    second = _run(server._move_to_absolute(1500, 900))
    assert second["converged"] is True, second
    assert second["iters"] <= first["iters"], (first, second)


def test_a_stale_gain_does_not_strand_a_short_move(server):
    """The counter-example that carrying the gain across moves creates.

    Learn a gain of 2.5, then have the host change under us (the user
    turns pointer speed down, an RDP session takes over, the mouse is
    swapped) so the real gain is 0.25. A short move now commands
    `24 / 2.5 = 10` px, which is under the sampling floor: the estimate
    can never be re-measured, and each pass creeps 2 px. Ten passes later
    it has gone nowhere — strictly worse than never having persisted the
    gain at all.

    The escape is that a pass which teaches nothing AND barely moves walks
    the estimate back toward 1.0, which makes the next command big enough
    to measure.
    """
    _install_overshoot_bridge(server, accel=2.5, start=(0, 0))
    first = _run(server._move_to_absolute(1500, 900))
    assert first["converged"] is True, first
    assert server._pointer_gain > 2.0, server._pointer_gain

    # Same server, different world: 10x less pointer movement per delta.
    _install_overshoot_bridge(server, accel=0.25, start=(1000, 600))
    result = _run(server._move_to_absolute(1024, 624))
    assert result["converged"] is True, (result, server._pointer_gain)
    assert abs(result["x"] - 1024) <= MOVE_TOLERANCE, result


def test_a_position_beyond_the_bounds_is_not_read_as_an_edge_clip(server):
    """The boundary test is equality, not a range.

    A cursor pinned AT the edge really was clipped, and its ratio
    understates the gain. A cursor reported BEYOND the edge was not
    clipped by anything — a real OS never puts it there — so discarding
    that sample threw away the only measurement available and left the
    loop unable to learn at all.
    """
    server.config.screen_w = 1920
    server.config.screen_h = 1080
    gain, _ = server._update_gain(
        1.0, (1000, 0, (0, 500)), (2500, 500), 4000, 500, None)
    assert abs(gain - 2.5) < 1e-6, gain

    pinned, _ = server._update_gain(
        1.0, (1000, 0, (1500, 500)), (1919, 500), 4000, 500, None)
    assert pinned == 1.0, "a pass pinned at the edge must not move the estimate"
