# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Regression tests: composed tools must NOT report success when an
underlying bridge sub-call fails.

The composed handlers (`hid.click`, `hid.hover`, `hid.drag`,
`hid.hold_key`) and the stepped-move helpers issue several bridge
sub-calls in sequence. Each sub-call returns a bool ACK (`False` =
firmware did not acknowledge — timeout / seq mismatch / firmware ERROR
/ parse error). A physical-control tool that swallowed a failed move
and clicked anyway — or that hard-coded ``ok: True`` over a failed
press — would tell the agent "done" while the screen did not change.
For an autonomous agent driving real hardware that is the worst class
of bug: it builds on a click that never landed.

These tests pin the contract:

  * a failed positioning step (move ACK false / no convergence /
    cursor unavailable) aborts the dependent action (no click, no
    drag press, ...) and surfaces the failure;
  * a failed action ACK (click / press / release) is reflected in
    ``ok`` and never masked by a later successful sub-call;
  * mid-gesture cleanup (button release / key release) still runs even
    when the gesture failed — a stuck button/modifier is worse than a
    reported failure;
  * the happy path is unaffected (``ok: True``).

Absolute moves have a cursor-verified feedback loop, so their ``ok``
comes from convergence (the OS cursor is ground truth); relative moves
have no feedback loop, so their ``ok`` is the AND of every report's
ACK. Both are exercised below.
"""
from __future__ import annotations

import asyncio

import pytest

from clawtouch_mcp import cursor as _cursor_mod
from clawtouch_mcp import server as _server_mod
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


def _count(server, name):
    return len([c for c in server.bridge._calls if c[0] == name])


def _patch_move(server, fn):
    """Replace MockBridge.mouse_move while still logging the call so
    the move-count assertions keep working."""
    async def wrapper(x, y, *, relative=False):
        server.bridge._calls.append(("move", {"x": x, "y": y, "relative": relative}))
        return await fn(x, y, relative)
    server.bridge.mouse_move = wrapper


# ── hid.click — absolute path ────────────────────────────────────────

def test_click_absolute_no_convergence_does_not_click(server):
    """Cursor ACKs every move but never actually moves → converge fails
    → ok=False and NO click is emitted (we never confirmed reaching the
    target)."""
    _cursor_mod._seed_fake_cursor(100, 100)

    async def stuck(x, y, relative):
        return True  # ACKs but does not update the fake cursor

    _patch_move(server, stuck)
    result = _run(server._tool_click(x=800, y=600))
    assert result["ok"] is False
    assert result["converged"] is False
    assert _count(server, "click") == 0


def test_click_absolute_cursor_unavailable_does_not_click(server, monkeypatch):
    """OS cursor query unavailable (e.g. Wayland) → structured error,
    no click."""
    monkeypatch.setattr(_server_mod, "get_cursor_position", lambda: None)
    _cursor_mod._clear_fake_cursor()
    result = _run(server._tool_click(x=500, y=400))
    assert "error" in result
    assert _count(server, "click") == 0


def test_click_absolute_move_acked_but_cursor_reached_still_clicks(server):
    """Edge: a move whose ACK is False but whose cursor DID land on the
    target still clicks — in absolute mode the OS cursor is ground truth,
    so ``move_acked=False`` is recorded as a diagnostic but does not block
    a click we can prove reached the target."""
    _cursor_mod._seed_fake_cursor(100, 100)

    async def moves_but_no_ack(x, y, relative):
        _cursor_mod._update_fake_cursor(x, y, relative=relative)
        return False  # firmware "didn't ACK" yet the cursor moved

    _patch_move(server, moves_but_no_ack)
    result = _run(server._tool_click(x=500, y=400))
    assert result["converged"] is True
    assert result.get("move_acked") is False  # diagnostic preserved
    assert result["ok"] is True               # click still emitted
    assert _count(server, "click") == 1


def test_click_click_ack_failure_is_not_masked(server):
    """Move lands but the click itself is not ACKed → ok=False."""
    _cursor_mod._seed_fake_cursor(100, 100)

    async def fail_click(button="left", *, double=False):
        server.bridge._calls.append(("click", {"button": button}))
        return False

    server.bridge.mouse_click = fail_click
    result = _run(server._tool_click(x=500, y=400))
    assert result["ok"] is False
    assert result["clicked"] is False


# ── hid.click — relative path (ACK is authoritative) ─────────────────

def test_click_relative_snap_move_failure_does_not_click(server):
    """Relative snap move not ACKed → ok=False, no click."""
    _cursor_mod._clear_fake_cursor()

    async def fail(x, y, relative):
        return False

    _patch_move(server, fail)
    result = _run(server._tool_click(x=40, y=40, relative=True))
    assert result["ok"] is False
    assert _count(server, "click") == 0


def test_click_relative_stepped_dropped_report_does_not_click(server):
    """One dropped report during a glided relative move → stepped move
    ok=False → no click."""
    _cursor_mod._clear_fake_cursor()
    state = {"n": 0}

    async def flaky(x, y, relative):
        state["n"] += 1
        return state["n"] != 2  # 2nd report drops

    _patch_move(server, flaky)
    result = _run(server._tool_click(x=200, y=150, relative=True, move_ms=100))
    assert result["ok"] is False
    assert _count(server, "click") == 0


# ── hid.hover ────────────────────────────────────────────────────────

def test_hover_no_convergence_reports_failure(server):
    _cursor_mod._seed_fake_cursor(100, 100)

    async def stuck(x, y, relative):
        return True

    _patch_move(server, stuck)
    result = _run(server._tool_hover(x=800, y=600, duration_ms=1))
    assert result["ok"] is False
    assert result["converged"] is False


def test_hover_cursor_unavailable_reports_error(server, monkeypatch):
    monkeypatch.setattr(_server_mod, "get_cursor_position", lambda: None)
    _cursor_mod._clear_fake_cursor()
    result = _run(server._tool_hover(x=500, y=400, duration_ms=1))
    assert "error" in result


# ── hid.drag ─────────────────────────────────────────────────────────

def test_drag_relative_source_move_failure_aborts_before_press(server):
    """If the move to the drag source is not ACKed, the button must NOT
    be pressed — pressing/dragging from an unconfirmed point is worse
    than not dragging."""
    _cursor_mod._clear_fake_cursor()

    async def fail(x, y, relative):
        return False

    _patch_move(server, fail)
    result = _run(server._tool_drag(
        from_x=10, from_y=10, to_x=20, to_y=20, relative=True, move_ms=0,
    ))
    assert result["ok"] is False
    assert result.get("stage") == "move_to_source"
    assert _count(server, "button_down") == 0
    assert _count(server, "button_up") == 0


def test_drag_absolute_source_no_convergence_aborts_before_press(server):
    _cursor_mod._seed_fake_cursor(100, 100)

    async def stuck(x, y, relative):
        return True  # ACKs, never moves → source converge fails

    _patch_move(server, stuck)
    result = _run(server._tool_drag(
        from_x=800, from_y=600, to_x=400, to_y=300, move_ms=0,
    ))
    assert result["ok"] is False
    assert _count(server, "button_down") == 0


def test_drag_destination_move_failure_still_releases_button(server):
    """Source move lands, button presses, destination move fails → drag
    ok=False but the button is STILL released (no stuck button)."""
    _cursor_mod._seed_fake_cursor(0, 0)
    state = {"n": 0}

    async def first_ok_then_fail(x, y, relative):
        state["n"] += 1
        if state["n"] == 1:  # source move ok
            _cursor_mod._update_fake_cursor(x, y, relative=relative)
            return True
        return False  # destination move drops

    _patch_move(server, first_ok_then_fail)
    result = _run(server._tool_drag(
        from_x=100, from_y=100, to_x=120, to_y=120, relative=True, move_ms=0,
    ))
    assert result["ok"] is False
    assert _count(server, "button_up") == 1  # released despite failure


def test_drag_button_down_ack_failure_is_not_masked(server):
    """A successful release must not upgrade a drag whose press failed."""
    _cursor_mod._seed_fake_cursor(0, 0)

    async def fail_down(button="left"):
        server.bridge._calls.append(("button_down", {"button": button}))
        return False

    server.bridge.mouse_button_down = fail_down
    result = _run(server._tool_drag(
        from_x=100, from_y=100, to_x=500, to_y=400, move_ms=0,
    ))
    assert result["ok"] is False
    assert result["down_acked"] is False
    assert _count(server, "button_up") == 1


def test_drag_button_up_ack_failure_is_not_masked(server):
    _cursor_mod._seed_fake_cursor(0, 0)

    async def fail_up(button="left"):
        server.bridge._calls.append(("button_up", {"button": button}))
        return False

    server.bridge.mouse_button_up = fail_up
    result = _run(server._tool_drag(
        from_x=100, from_y=100, to_x=500, to_y=400, move_ms=0,
    ))
    assert result["ok"] is False
    assert result["up_acked"] is False


# ── hid.hold_key ─────────────────────────────────────────────────────

def test_hold_key_press_failure_still_releases_and_reports_failure(server):
    async def fail_press(key, modifiers=None):
        server.bridge._calls.append(("key_press", {"key": key}))
        return False

    server.bridge.key_press = fail_press
    result = _run(server._tool_hold_key(key="a", duration_ms=2))
    assert result["ok"] is False
    assert result["press_acked"] is False
    assert _count(server, "key_release") == 1  # released anyway


def test_hold_key_release_failure_is_not_masked(server):
    async def fail_release(key="", modifiers=None):
        server.bridge._calls.append(("key_release", {"key": key}))
        return False

    server.bridge.key_release = fail_release
    result = _run(server._tool_hold_key(key="a", duration_ms=2))
    assert result["ok"] is False
    assert result["release_acked"] is False


# ── stepped relative move helper (ACK propagation) ───────────────────

def test_stepped_relative_move_ok_true_when_all_acked(server):
    _cursor_mod._clear_fake_cursor()
    result = _run(server._stepped_relative_move(200, 150, move_ms=100))
    assert result["ok"] is True


def test_stepped_relative_move_ok_false_when_a_report_drops(server):
    _cursor_mod._clear_fake_cursor()
    state = {"n": 0}

    async def flaky(x, y, relative):
        state["n"] += 1
        return state["n"] != 3  # 3rd report drops

    _patch_move(server, flaky)
    result = _run(server._stepped_relative_move(200, 150, move_ms=100))
    assert result["ok"] is False


# ── happy paths unaffected ───────────────────────────────────────────

def test_happy_click_absolute_ok(server):
    _cursor_mod._seed_fake_cursor(100, 100)
    result = _run(server._tool_click(x=500, y=400))
    assert result["ok"] is True
    assert result["clicked"] is True


def test_happy_drag_absolute_ok(server):
    _cursor_mod._seed_fake_cursor(0, 0)
    result = _run(server._tool_drag(
        from_x=100, from_y=100, to_x=500, to_y=400, move_ms=0,
    ))
    assert result["ok"] is True
    assert result["down_acked"] is True
    assert result["up_acked"] is True


def test_happy_hold_key_ok(server):
    result = _run(server._tool_hold_key(key="a", duration_ms=2))
    assert result["ok"] is True
    assert result["press_acked"] is True
    assert result["release_acked"] is True
