# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""hid.batch — sequence a short pre-planned action list in one call.

These tests pin the four safety properties that make hid.batch more than
a convenience wrapper (see the design notes on `_tool_batch`):

  1. one rate-check for the whole batch; per-op work goes through the
     standalone tools' leaf helpers, so an op never raises a *rate*
     RuntimeError mid-batch;
  2. a raised exception inside one op (text-too-long ValueError, a bridge
     method blowing up, …) becomes that op's {ok:false, error} entry and
     never escapes to discard the whole batch;
  3. on abnormal termination (stop_on_error halts after a press), held
     buttons/keys are released; a CLEAN run is NOT auto-released;
  4. the result always carries a top-level `ok` so a partial failure
     surfaces as isError:true through dispatch with results intact.

Plus the hard op cap (10), the empty-batch base case, strict ordering,
and that per-op convergence diagnostics survive.

MockBridge (cursor-reflux) means absolute moves converge in one pass, so
the happy paths don't need real hardware; the failure paths use the same
`_patch_move` / `_seed_fake_cursor` helpers as
test_composed_tool_failure_propagation.py.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from clawtouch_mcp import cursor as _cursor_mod
from clawtouch_mcp import server as _server_mod
from clawtouch_mcp.server import (
    DEFAULT_CLICK_SETTLE_MS,
    MAX_BATCH_DELAY_MS,
    MAX_BATCH_OPS,
    MAX_TYPE_LEN,
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
    async def wrapper(x, y, *, relative=False):
        server.bridge._calls.append(("move", {"x": x, "y": y, "relative": relative}))
        return await fn(x, y, relative)
    server.bridge.mouse_move = wrapper


# ── base cases ───────────────────────────────────────────────────────

def test_empty_batch_is_ok(server):
    result = _run(server._tool_batch(ops=[]))
    assert result["ok"] is True
    assert result["results"] == []
    assert result["count"] == 0
    assert result["failed_index"] is None
    assert result["stopped_early"] is False
    assert result["released_all"] is False


def test_missing_ops_defaults_to_empty(server):
    # ops is required by schema, but the handler tolerates omission as []
    result = _run(server._tool_batch())
    assert result["ok"] is True
    assert result["results"] == []


# ── happy paths ──────────────────────────────────────────────────────

def test_happy_ten_op_batch_all_ok_no_rate_limit(server):
    # 10 scroll ops in one batch must not trip the 20 ops/sec limiter
    # (the batch rate-checks ONCE, not per op).
    ops = [{"type": "scroll", "delta": 1} for _ in range(MAX_BATCH_OPS)]
    result = _run(server._tool_batch(ops=ops))
    assert result["ok"] is True
    assert result["count"] == MAX_BATCH_OPS
    assert _count(server, "scroll") == MAX_BATCH_OPS
    assert all(r["ok"] for r in result["results"])


def test_ops_run_in_strict_order(server):
    _cursor_mod._seed_fake_cursor(100, 100)
    ops = [
        {"type": "scroll", "delta": 5},
        {"type": "type", "text": "hi"},
        {"type": "key", "key": "enter"},
    ]
    result = _run(server._tool_batch(ops=ops))
    assert result["ok"] is True
    kinds = [c[0] for c in server.bridge._calls]
    assert kinds == ["scroll", "type", "key"]
    # indices are preserved and dense
    assert [r["index"] for r in result["results"]] == [0, 1, 2]


def test_click_op_reuses_converge_and_clicks(server):
    _cursor_mod._seed_fake_cursor(100, 100)
    result = _run(server._tool_batch(ops=[{"type": "click", "x": 500, "y": 400}]))
    assert result["ok"] is True
    op = result["results"][0]
    assert op["type"] == "click"
    assert op["converged"] is True
    assert op["clicked"] is True
    assert _count(server, "click") == 1


def test_relative_click_op_has_no_converged_field(server):
    _cursor_mod._clear_fake_cursor()
    result = _run(server._tool_batch(
        ops=[{"type": "click", "x": 40, "y": 40, "relative": True}],
    ))
    assert result["ok"] is True
    op = result["results"][0]
    assert op.get("relative") is True
    assert "converged" not in op  # relative path has no cursor feedback loop
    assert op["clicked"] is True


def test_key_op_parses_shorthand(server):
    result = _run(server._tool_batch(ops=[{"type": "key", "key": "ctrl+c"}]))
    assert result["ok"] is True
    key_calls = [c for c in server.bridge._calls if c[0] == "key"]
    assert key_calls[0][1]["modifiers"] == ["ctrl"]
    assert key_calls[0][1]["key"] == "c"


def test_type_op_reports_sent_chars(server):
    result = _run(server._tool_batch(ops=[{"type": "type", "text": "ab\ncd"}]))
    assert result["ok"] is True
    # control char (\n) is stripped on the wire — reported as 4, not 5
    assert result["results"][0]["chars"] == 4


# ── per-op diagnostics survive ───────────────────────────────────────

def test_per_op_converge_diagnostics_survive(server):
    """A click whose cursor never reaches target keeps its converged /
    residual / move_acked diagnostics in the per-op result."""
    _cursor_mod._seed_fake_cursor(100, 100)

    async def stuck(x, y, relative):
        return True  # ACKs but never moves the fake cursor

    _patch_move(server, stuck)
    result = _run(server._tool_batch(ops=[{"type": "click", "x": 800, "y": 600}]))
    op = result["results"][0]
    assert op["ok"] is False
    assert op["converged"] is False
    assert "residual_x" in op and "residual_y" in op
    assert "move_acked" in op
    assert _count(server, "click") == 0  # never clicked an unconfirmed point


# ── exception isolation (must-fix #2 + #4) ───────────────────────────

def test_op_exception_becomes_op_error_not_batch_crash(server):
    """A too-long type raises ValueError inside the op; it must surface as
    that op's {ok:false, error} and not abort the whole batch (the later
    ops still run when stop_on_error=false)."""
    ops = [
        {"type": "scroll", "delta": 1},
        {"type": "type", "text": "x" * (MAX_TYPE_LEN + 1)},   # raises ValueError
        {"type": "scroll", "delta": 2},
    ]
    result = _run(server._tool_batch(ops=ops, stop_on_error=False))
    assert result["ok"] is False
    assert result["failed_index"] == 1
    assert result["count"] == 3                      # all three ran
    assert result["results"][1]["ok"] is False
    assert "error" in result["results"][1]
    assert "too long" in result["results"][1]["error"]
    assert result["results"][2]["ok"] is True        # batch did not crash


def test_bridge_exception_is_caught_per_op(server):
    async def boom(text, *, chunk_size=32, allow_control=False):
        raise RuntimeError("bridge exploded")

    server.bridge.type_text = boom
    result = _run(server._tool_batch(ops=[{"type": "type", "text": "hi"}]))
    assert result["ok"] is False
    assert result["results"][0]["ok"] is False
    assert "bridge exploded" in result["results"][0]["error"]


def test_unknown_op_type_becomes_op_error(server):
    result = _run(server._tool_batch(
        ops=[{"type": "teleport", "x": 1, "y": 2}], stop_on_error=False,
    ))
    assert result["ok"] is False
    assert "unknown batch op type" in result["results"][0]["error"]


# ── stop_on_error semantics ──────────────────────────────────────────

def test_stop_on_error_true_halts_at_first_failure(server):
    ops = [
        {"type": "scroll", "delta": 1},
        {"type": "type", "text": "x" * (MAX_TYPE_LEN + 1)},   # fails
        {"type": "scroll", "delta": 2},                       # must NOT run
    ]
    result = _run(server._tool_batch(ops=ops, stop_on_error=True))
    assert result["ok"] is False
    assert result["stopped_early"] is True
    assert result["failed_index"] == 1
    assert result["count"] == 2          # third op never executed
    assert _count(server, "scroll") == 1  # only the first scroll ran


def test_stop_on_error_false_runs_everything(server):
    ops = [
        {"type": "type", "text": "x" * (MAX_TYPE_LEN + 1)},   # fails
        {"type": "scroll", "delta": 2},
    ]
    result = _run(server._tool_batch(ops=ops, stop_on_error=False))
    assert result["ok"] is False
    assert result["stopped_early"] is False
    assert result["count"] == 2
    assert _count(server, "scroll") == 1


# ── held-state cleanup (must-fix #3) ─────────────────────────────────

def test_cleanup_release_all_when_stopped_after_press(server):
    """button_down succeeds, then a click fails to converge → stop_on_error
    halts the run → release_all fires so the button isn't left held."""
    _cursor_mod._seed_fake_cursor(100, 100)

    async def stuck(x, y, relative):
        return True

    _patch_move(server, stuck)
    ops = [
        {"type": "button_down", "button": "left"},
        {"type": "click", "x": 800, "y": 600},   # never converges → fails
    ]
    result = _run(server._tool_batch(ops=ops, stop_on_error=True))
    assert result["ok"] is False
    assert result["stopped_early"] is True
    assert result["released_all"] is True
    assert _count(server, "release_all") == 1


def test_clean_run_does_not_auto_release_held_button(server):
    """A batch that completes cleanly may intentionally leave a button
    held for a follow-up call — it must NOT auto-release."""
    result = _run(server._tool_batch(ops=[{"type": "button_down", "button": "left"}]))
    assert result["ok"] is True
    assert result["released_all"] is False
    assert _count(server, "release_all") == 0


def test_no_cleanup_when_failure_had_no_press(server):
    """Failure with no prior button_down/key press → nothing to release."""
    ops = [{"type": "type", "text": "x" * (MAX_TYPE_LEN + 1)}]
    result = _run(server._tool_batch(ops=ops, stop_on_error=True))
    assert result["ok"] is False
    assert result["released_all"] is False
    assert _count(server, "release_all") == 0


# ── hard op cap ──────────────────────────────────────────────────────

def test_too_many_ops_rejected(server):
    ops = [{"type": "scroll", "delta": 1} for _ in range(MAX_BATCH_OPS + 1)]
    with pytest.raises(ValueError, match="at most"):
        _run(server._tool_batch(ops=ops))
    # nothing was sent — refused at the boundary
    assert _count(server, "scroll") == 0


# ── dispatch wiring: partial failure → isError:true (must-fix #4) ─────

def test_dispatch_partial_failure_is_iserror_with_results(server):
    ops = [
        {"type": "scroll", "delta": 1},
        {"type": "type", "text": "x" * (MAX_TYPE_LEN + 1)},   # fails
    ]
    resp = _run(server.dispatch({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "hid.batch",
                   "arguments": {"ops": ops, "stop_on_error": False}},
    }))
    result = resp["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["failed_index"] == 1
    assert len(payload["results"]) == 2          # full per-op array preserved


def test_dispatch_all_ok_is_not_iserror(server):
    resp = _run(server.dispatch({
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "hid.batch",
                   "arguments": {"ops": [{"type": "scroll", "delta": 1}]}},
    }))
    result = resp["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True


# ── review fixes: cleanup precision, diagnostics, delay coverage ─────

def test_key_op_does_not_trigger_cleanup(server):
    """`key` is atomic (key_combo press+release) — it leaves nothing held,
    so a failure after a key op must NOT fire release_all (regression for
    the 'key marked pressed_something' false-cleanup)."""
    ops = [
        {"type": "key", "key": "ctrl+c"},
        {"type": "type", "text": "x" * (MAX_TYPE_LEN + 1)},   # fails
    ]
    result = _run(server._tool_batch(ops=ops, stop_on_error=True))
    assert result["ok"] is False
    assert result["stopped_early"] is True
    assert result["released_all"] is False
    assert "cleanup_error" not in result
    assert _count(server, "release_all") == 0


def test_non_exception_failure_carries_bridge_diagnostic(server):
    """A leaf returning ok:False (not raising) still gets the bridge's
    specific diagnostic attached to its per-op result."""
    _cursor_mod._clear_fake_cursor()
    server.bridge.last_error_detail = "ACK timeout seq=5"

    async def fail(x, y, relative):
        return False  # firmware did not ACK

    _patch_move(server, fail)
    result = _run(server._tool_batch(
        ops=[{"type": "click", "x": 40, "y": 40, "relative": True}],
    ))
    op = result["results"][0]
    assert op["ok"] is False
    assert op["bridge_diagnostic"] == "ACK timeout seq=5"


def test_cleanup_error_surfaced_when_release_all_fails(server):
    """If release_all itself raises during cleanup, released_all stays False
    but a cleanup_error field distinguishes 'attempted-and-failed' from
    'not needed' — and the batch still returns its per-op results."""
    async def boom_release():
        raise RuntimeError("hardware gone")

    server.bridge.release_all = boom_release
    ops = [
        {"type": "button_down", "button": "left"},
        {"type": "type", "text": "x" * (MAX_TYPE_LEN + 1)},   # fails → stop
    ]
    result = _run(server._tool_batch(ops=ops, stop_on_error=True))
    assert result["ok"] is False
    assert result["stopped_early"] is True
    assert result["released_all"] is False
    assert "cleanup_error" in result
    assert "hardware gone" in result["cleanup_error"]
    assert len(result["results"]) == 2          # results NOT discarded


def test_delay_ms_is_honored(server):
    """A non-zero delay_ms runs the post-op sleep path without error."""
    result = _run(server._tool_batch(
        ops=[{"type": "scroll", "delta": 1, "delay_ms": 1}],
    ))
    assert result["ok"] is True
    assert _count(server, "scroll") == 1


# ── click-settle gap: back-to-back clicks aren't coalesced by the OS ──

def test_op_settle_ms_defaults(server):
    """Click/button ops default to the settle gap; others to 0."""
    assert server._op_settle_ms({"type": "click"}) == DEFAULT_CLICK_SETTLE_MS
    assert server._op_settle_ms({"type": "button_down"}) == DEFAULT_CLICK_SETTLE_MS
    assert server._op_settle_ms({"type": "button_up"}) == DEFAULT_CLICK_SETTLE_MS
    assert server._op_settle_ms({"type": "move"}) == 0
    assert server._op_settle_ms({"type": "scroll"}) == 0
    assert server._op_settle_ms({"type": "type"}) == 0
    assert server._op_settle_ms({"type": "key"}) == 0


def test_op_settle_ms_explicit_overrides_default(server):
    assert server._op_settle_ms({"type": "click", "delay_ms": 0}) == 0
    assert server._op_settle_ms({"type": "click", "delay_ms": 200}) == 200
    assert server._op_settle_ms({"type": "scroll", "delay_ms": 100}) == 100
    # clamp to the ceiling + non-int coerces to 0 (never crashes)
    assert server._op_settle_ms({"type": "click", "delay_ms": 999999}) == MAX_BATCH_DELAY_MS
    assert server._op_settle_ms({"type": "click", "delay_ms": "x"}) == 0


def test_consecutive_clicks_get_default_settle_gap(server, monkeypatch):
    """Three back-to-back clicks insert a default gap BETWEEN ops (2 gaps
    for 3 ops — none after the last). Relative clicks have no converge
    sleeps, so the only sleeps recorded are the settle gaps."""
    sleeps: list[float] = []

    async def rec(d):
        sleeps.append(d)

    monkeypatch.setattr(_server_mod.asyncio, "sleep", rec)
    _cursor_mod._clear_fake_cursor()
    ops = [{"type": "click", "x": 0, "y": 10, "relative": True} for _ in range(3)]
    result = _run(server._tool_batch(ops=ops))
    assert result["ok"] is True
    gap = DEFAULT_CLICK_SETTLE_MS / 1000.0
    assert [d for d in sleeps if d == gap] == [gap, gap]   # exactly 2, not 3


def test_explicit_zero_delay_disables_settle_gap(server, monkeypatch):
    """An advanced caller can opt out of the default gap with delay_ms:0."""
    sleeps: list[float] = []

    async def rec(d):
        sleeps.append(d)

    monkeypatch.setattr(_server_mod.asyncio, "sleep", rec)
    _cursor_mod._clear_fake_cursor()
    ops = [
        {"type": "click", "x": 0, "y": 10, "relative": True, "delay_ms": 0},
        {"type": "click", "x": 0, "y": 10, "relative": True, "delay_ms": 0},
    ]
    result = _run(server._tool_batch(ops=ops))
    assert result["ok"] is True
    assert sleeps == []   # no gap inserted
