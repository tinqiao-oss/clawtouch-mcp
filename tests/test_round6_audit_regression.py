"""Regression tests for round 6 audit fixes (codex external review).

Round 6 (2026-05-26) codex external audit found 6 high/medium issues
(3 H + 3 M) that 5-agent internal round 5 audit missed. All were
verified true, fixed in a single commit. This file pins the behaviour
so the bugs can't silently reappear.

Coverage:

1. ``SerialHidBridge.key_combo`` no longer raises NameError on the
   real-hardware path when called with a shifted-glyph name like
   ``"plus"`` / ``"tilde"`` / ``"quote"``. MockBridge replaces
   ``key_combo`` wholesale so the existing server tests don't go
   through the real method at all — this test reaches into the real
   class so a missing import is caught.

2. ``_on_tool_call`` translates ``{"ok": False, ...}`` results from
   bridge calls into ``isError: true`` MCP responses, with the
   bridge's last_error_detail enriched into the content. Previously
   a timeout / firmware-ERROR / parse-error would surface as
   ``isError: false, {"ok": false}`` and the agent would think the
   call succeeded.

3. ``_idle_watch`` defers idle release while any tool handler is
   in-flight. A 4096-char ``type_text`` or a stalled hardware
   response can outlive the idle deadline; previously the watcher
   would close the serial port mid-command and the firmware would
   be left in a bad state.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from clawtouch_mcp.bridge import SerialHidBridge
from clawtouch_mcp.server import ClawTouchMcpServer, ServerConfig


# ── 1. name_needs_shift NameError ──

class TestKeyComboShiftedAliasNoNameError:
    """Round 4 added shifted-glyph aliases (plus / tilde / quote) to
    keycodes.py and made bridge.key_combo OR-in SHIFT for them. The
    bridge call to ``name_needs_shift`` slipped past CI because the
    import line was forgotten and MockBridge fully replaces key_combo.
    Round 6 (codex external audit) caught the NameError — this test
    pins the import path.
    """

    @pytest.mark.parametrize("named_key", ["plus", "tilde", "quote", "enter"])
    def test_shifted_alias_does_not_raise_nameerror(self, named_key, monkeypatch):
        # Stand up a real SerialHidBridge but stub the wire I/O so the
        # method body runs end-to-end without touching hardware. The
        # whole point is to exercise the import path the NameError
        # was hiding on.
        bridge = SerialHidBridge("/dev/null", baudrate=115200)
        bridge._send_raw = AsyncMock(return_value=SimpleNamespace(
            cmd_type=__import__(
                "clawtouch_mcp.protocol", fromlist=["CommandType"]
            ).CommandType.ACK,
            seq_id=1,
        ))
        # Should not raise NameError on the shifted-name code path.
        ok = asyncio.run(bridge.key_combo([], named_key))
        assert ok is True

    def test_name_needs_shift_is_imported_in_bridge_module(self):
        # Belt-and-braces: explicit import-symbol check at the module
        # level so a future refactor that drops the import again fails
        # loudly even if the parametric test above goes flaky.
        from clawtouch_mcp import bridge as bridge_mod
        assert hasattr(bridge_mod, "name_needs_shift"), (
            "bridge.py must import name_needs_shift from keycodes "
            "(used in key_combo for shifted-glyph alias handling)"
        )


# ── 2. _on_tool_call isError on ok=False ──

class TestToolCallSurfacesBridgeFailureAsIsError:
    """Bridge methods return False on wire-level failure (timeout /
    seq mismatch / firmware ERROR / parse error) and set
    ``last_error_detail`` with the specific reason. The MCP server
    must translate ``{"ok": False}`` results into ``isError: true``
    with the diagnostic surfaced into content — otherwise the agent
    sees ``isError: false`` and believes the click landed.
    """

    def _server_with_failing_bridge(self, detail: str = "ACK timeout after 1.0s"):
        config = ServerConfig(port="/dev/null", mock=True)
        server = ClawTouchMcpServer(config)
        # Replace MockBridge with a stub that returns ok=False.
        server.bridge = SimpleNamespace(
            mouse_click=AsyncMock(return_value=False),
            type_text=AsyncMock(return_value=False),
            last_error_detail=detail,
        )
        return server

    def test_ok_false_becomes_iserror_true(self):
        server = self._server_with_failing_bridge()
        resp = asyncio.run(server._on_tool_call({
            "name": "hid.type",
            "arguments": {"text": "hello"},
        }))
        assert resp["isError"] is True, (
            "Bridge ok=False must surface as MCP isError=true so the "
            "agent can react; previously it silently passed as success."
        )

    def test_bridge_diagnostic_in_content(self):
        server = self._server_with_failing_bridge("ACK timeout after 1.0s")
        resp = asyncio.run(server._on_tool_call({
            "name": "hid.type",
            "arguments": {"text": "hello"},
        }))
        body = json.loads(resp["content"][0]["text"])
        assert body.get("bridge_diagnostic") == "ACK timeout after 1.0s", (
            "last_error_detail must be embedded in tool result so the "
            "agent can decide whether to retry or escalate"
        )

    def test_ok_true_still_iserror_false(self):
        # Sanity: the new ok=False guard mustn't flip happy-path
        # successes to isError too.
        config = ServerConfig(port="/dev/null", mock=True)
        server = ClawTouchMcpServer(config)
        server.bridge = SimpleNamespace(
            type_text=AsyncMock(return_value=True),
            last_error_detail=None,
        )
        resp = asyncio.run(server._on_tool_call({
            "name": "hid.type",
            "arguments": {"text": "hello"},
        }))
        assert resp["isError"] is False


# ── 3. _idle_watch in-flight protection ──

class TestIdleWatchHonoursInFlightHandlers:
    """A slow ``hid.type`` (4096 chars × multi-chunk) or a stalled
    hardware response can take longer than ``idle_close_after``.
    Releasing the bridge mid-command leaves the firmware in a bad
    state. The watcher must defer release while in-flight handlers
    are running.
    """

    def test_watcher_skips_release_while_handler_in_flight(self, monkeypatch):
        config = ServerConfig(
            port="/dev/null",
            mock=True,
            idle_close_after=0.05,       # 50ms idle deadline
            idle_check_interval=0.01,    # 10ms watcher tick
        )
        server = ClawTouchMcpServer(config)
        # Pretend a real SerialHidBridge is mounted so the watcher
        # considers the bridge releaseable.
        server.bridge = SerialHidBridge("/dev/null", baudrate=115200)
        release_calls = {"count": 0}

        async def _spy_release():
            release_calls["count"] += 1

        monkeypatch.setattr(server, "_idle_release_now", _spy_release)

        async def _run():
            server._inflight_handlers = 1  # simulate handler mid-call
            server._last_used_at = 0       # force "way past deadline"
            task = asyncio.create_task(server._idle_watch())
            await asyncio.sleep(0.08)      # ~8 watcher ticks
            assert release_calls["count"] == 0, (
                "watcher must NOT close the bridge while a handler "
                f"is in flight; got {release_calls['count']} releases"
            )
            # Now simulate handler returning — watcher should release
            # on the very next tick.
            server._inflight_handlers = 0
            server._last_used_at = 0
            await asyncio.sleep(0.05)
            assert release_calls["count"] >= 1, (
                "watcher must release once the in-flight count drops "
                "back to 0 and the deadline is still exceeded"
            )
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())
