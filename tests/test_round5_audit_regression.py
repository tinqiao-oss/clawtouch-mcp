# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Regression tests for round 5 audit fixes.

Round 5 (2026-05-26) fixed five Python-side issues spotted by a multi-agent
deep audit that round 4 missed. Each fix here gets a dedicated test so the
behaviour can't silently regress later:

1. ``_read_framed`` rejects bogus Content-Length values (negative, zero,
   or above ``MAX_FRAME_LEN``) instead of trying to read them.
2. ``SerialHidBridge.connect()`` resets ``_seq`` to 0 on every (re)connect
   so stale-ACK defences see a clean counter after a USB unplug/replug.
3. ``_idle_watch`` catches generic exceptions, logs them, and clears the
   task slot so the next tool call can restart the watcher instead of
   silently holding the serial port forever.

The firmware ``buf.find()`` / ``del buf[:idx]`` fix in
``firmware/code.py`` and the CI cross-repo install fix in
``.github/workflows/ci.yml`` aren't directly unit-testable (real Pico
hardware / live GitHub Actions, respectively) — covered by other means.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from clawtouch_mcp import server as srv_mod
from clawtouch_mcp.bridge import SerialHidBridge
from clawtouch_mcp.server import (
    MAX_FRAME_LEN,
    ClawTouchMcpServer,
    ServerConfig,
    _read_framed,
)


# ── 1. Content-Length bounds ──

class TestReadFramedRejectsBogusLength:
    """``_read_framed`` should ValueError before allocating on bad lengths.

    The error propagates up to ``run_stdio``'s parse-error handler, which
    emits a JSON-RPC -32700 and keeps the session alive.
    """

    def test_negative_length_raises(self):
        with pytest.raises(ValueError, match="invalid Content-Length"):
            asyncio.run(_read_framed(-1))

    def test_zero_length_raises(self):
        with pytest.raises(ValueError, match="invalid Content-Length"):
            asyncio.run(_read_framed(0))

    def test_length_at_cap_is_rejected(self):
        # MAX_FRAME_LEN + 1 must be rejected. We don't actually want to
        # allocate MAX_FRAME_LEN bytes in a unit test, so we test the
        # cap directly.
        with pytest.raises(ValueError, match="exceeds MAX_FRAME_LEN"):
            asyncio.run(_read_framed(MAX_FRAME_LEN + 1))

    def test_huge_length_is_rejected_fast(self):
        # The whole point of the cap is that a malicious Content-Length
        # header doesn't get to trigger a multi-GB allocation. Sanity-
        # check that the rejection is immediate (well under a second)
        # for an absurd value.
        import time
        t0 = time.monotonic()
        with pytest.raises(ValueError):
            asyncio.run(_read_framed(99_999_999_999_999))
        assert time.monotonic() - t0 < 0.5, (
            "MAX_FRAME_LEN guard should reject before any allocation"
        )


# ── 2. Bridge seq reset on reconnect ──

class TestBridgeSeqResetOnConnect:
    """``connect()`` must reset ``_seq`` to 0.

    After a USB unplug/replug the firmware end resets its own seq state
    (because it re-enumerates). If the host kept counting upward, the
    first request after reconnect could carry a high seq value while
    firmware is back near zero, raising the stale-ACK ambiguity surface.
    """

    def test_seq_reset_after_simulated_reconnect(self, monkeypatch):
        # Don't actually open a serial port — just check the reset hook.
        bridge = SerialHidBridge("/dev/null", baudrate=115200)
        # Simulate a long-running session having advanced the counter.
        bridge._seq = 0xFEFE

        # Stub out the IO bits so connect() can run without hardware.
        class _FakeSerial:
            is_open = True
            in_waiting = 0
            def reset_input_buffer(self): pass

        async def _fake_executor(_loop, func):
            return _FakeSerial()

        async def _run():
            loop = asyncio.get_running_loop()
            monkeypatch.setattr(
                loop, "run_in_executor",
                lambda _exec, fn: _fake_executor(loop, fn),
            )
            await bridge.connect()

        asyncio.run(_run())
        assert bridge._seq == 0, (
            f"connect() should reset _seq to 0, got {bridge._seq:#x}"
        )

    def test_first_next_seq_after_reconnect_is_one(self, monkeypatch):
        # Concrete check: after reconnect, the first wire-level seq is 1
        # (counter resets to 0, _next_seq increments before returning).
        bridge = SerialHidBridge("/dev/null", baudrate=115200)
        bridge._seq = 0xFFFE  # near wrap boundary

        class _FakeSerial:
            is_open = True
            in_waiting = 0
            def reset_input_buffer(self): pass

        async def _fake_executor(_loop, _func):
            return _FakeSerial()

        async def _run():
            loop = asyncio.get_running_loop()
            monkeypatch.setattr(
                loop, "run_in_executor",
                lambda _exec, fn: _fake_executor(loop, fn),
            )
            await bridge.connect()

        asyncio.run(_run())
        assert bridge._next_seq() == 1


# ── 3. _idle_watch crash recovery ──

class TestIdleWatchCrashRecovery:
    """``_idle_watch`` must not silently die on unexpected exceptions.

    If it does, ``_ensure_idle_watch_started`` checks
    ``_idle_task.done()`` and refuses to restart because the slot is
    still occupied by a finished task. End result: the serial port is
    held forever. The fix logs the exception, clears the slot, and
    relies on the next tool call to restart the watcher.
    """

    def test_unexpected_exception_clears_slot_and_logs(self, caplog, monkeypatch):
        config = ServerConfig(
            port="/dev/null",
            mock=True,
            idle_close_after=1.0,
            idle_check_interval=0.01,
        )
        server = ClawTouchMcpServer(config)

        real_isinstance = isinstance

        def _boom(obj, cls):
            if cls is SerialHidBridge:
                raise RuntimeError("synthetic boom")
            return real_isinstance(obj, cls)

        # Force a crash inside the watch loop by shadowing isinstance ONLY
        # in the server module's namespace (_idle_watch does
        # `isinstance(self.bridge, SerialHidBridge)`). monkeypatch
        # auto-restores at teardown — no process-wide builtins mutation that
        # a skipped `finally` (or pytest-xdist reordering) could leak into
        # later tests.
        import clawtouch_mcp.server as srv_mod
        monkeypatch.setattr(srv_mod, "isinstance", _boom, raising=False)

        async def _run():
            with caplog.at_level(logging.ERROR, logger="clawtouch_mcp.server"):
                server._idle_task = asyncio.create_task(server._idle_watch())
                # Give the watcher one tick to wake up and crash.
                await asyncio.sleep(0.05)

        asyncio.run(_run())

        # Slot must be cleared so the next tool call can restart.
        assert server._idle_task is None, (
            "watcher crash should clear _idle_task so next call restarts it"
        )
        # And the exception must surface in logs, not vanish.
        assert any(
            "synthetic boom" in r.message or "_idle_watch crashed" in r.message
            for r in caplog.records
        ), "watcher crash should be logged with exc_info"
