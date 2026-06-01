# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Release-on-idle — mcp 让出 COM 串口给其他进程的核心 UX.

场景: ClawTouch desktop + clawtouch-mcp 共用同一块 Pico, 一次只能一个进程
持有 COM. 不加 release-on-idle 时 mcp 一启动就长占, ClawTouch 永远抢不到.
开启后 30s 无 tool call → close COM 替换 self.bridge 为 UnavailableBridge,
ClawTouch 可立即拿到; mcp 下次 tool call 由 UnavailableBridge._try_promote
自动 lazy reconnect (~50-200ms 透明恢复).
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from clawtouch_mcp.server import (
    ClawTouchMcpServer,
    MockBridge,
    SerialHidBridge,
    ServerConfig,
    UnavailableBridge,
)


def _mk_server(*, mock: bool = False, idle_close_after: float = 30.0,
                idle_check_interval: float = 0.05) -> ClawTouchMcpServer:
    """idle_check_interval=0.05 让 watcher 50ms 醒一次, 测试不用等 1s."""
    cfg = ServerConfig(
        port=None, baudrate=115200, mock=mock,
        screen_w=1920, screen_h=1080,
        idle_close_after=idle_close_after,
        idle_check_interval=idle_check_interval,
    )
    return ClawTouchMcpServer(cfg)


# ── _ensure_idle_watch_started ─────────────────────────────────────────


def test_disabled_when_idle_close_after_zero():
    """idle_close_after=0 → 永不启动 watcher (用户显式关此功能)."""
    server = _mk_server(idle_close_after=0)
    server.bridge = MagicMock(spec=SerialHidBridge)
    server._ensure_idle_watch_started()
    assert server._idle_task is None


def test_skipped_for_mock_bridge():
    """MockBridge 不需要 release (本来就不占硬件), 不启动 watcher."""
    server = _mk_server()
    server.bridge = MockBridge()
    server._ensure_idle_watch_started()
    assert server._idle_task is None


def test_skipped_for_unavailable_bridge():
    """UnavailableBridge 本来就没占串口, 启 watcher 无意义."""
    server = _mk_server()
    server.bridge = UnavailableBridge(server, ["COM6"], 115200)
    server._ensure_idle_watch_started()
    assert server._idle_task is None


# ── _idle_release_now ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idle_release_replaces_bridge_with_unavailable():
    """SerialHidBridge → close() + 替换为 UnavailableBridge, port 记到 tried_ports."""
    server = _mk_server()
    fake_serial = AsyncMock(spec=SerialHidBridge)
    fake_serial.port = "COM6"
    fake_serial.close = AsyncMock()
    server.bridge = fake_serial

    await server._idle_release_now()

    fake_serial.close.assert_awaited_once()
    assert isinstance(server.bridge, UnavailableBridge)
    assert server.bridge._tried_ports == ["COM6"]


@pytest.mark.asyncio
async def test_idle_release_swallows_close_exception():
    """close() 抛异常时仍替换为 UnavailableBridge (一致状态不阻塞 release)."""
    server = _mk_server()
    fake_serial = AsyncMock(spec=SerialHidBridge)
    fake_serial.port = "COM6"
    fake_serial.close = AsyncMock(side_effect=Exception("disconnected mid-flight"))
    server.bridge = fake_serial

    await server._idle_release_now()  # should not raise

    assert isinstance(server.bridge, UnavailableBridge)


@pytest.mark.asyncio
async def test_idle_release_noop_for_non_serial():
    """已经是 MockBridge/UnavailableBridge → no-op (幂等)."""
    server = _mk_server()
    mock_bridge = MockBridge()
    server.bridge = mock_bridge
    await server._idle_release_now()
    assert server.bridge is mock_bridge  # 不动


# ── _on_tool_call lazy 启动 watcher + 更新 _last_used_at ────────────────


@pytest.mark.asyncio
async def test_tool_call_updates_last_used_at():
    server = _mk_server(idle_close_after=0)  # 关 watcher 避免后台干扰
    server.bridge = MockBridge()
    server.tools = {"test": MagicMock(handler=AsyncMock(return_value={"ok": True}))}
    before = time.monotonic()
    await server._on_tool_call({"name": "test", "arguments": {}})
    assert server._last_used_at >= before


@pytest.mark.asyncio
async def test_tool_call_lazy_starts_idle_watch():
    """第一次 tool call 启动 idle watch (前提: bridge 是 SerialHidBridge)."""
    server = _mk_server(idle_close_after=1.0)
    fake_serial = AsyncMock(spec=SerialHidBridge)
    fake_serial.port = "COM6"
    server.bridge = fake_serial
    server.tools = {"test": MagicMock(handler=AsyncMock(return_value={"ok": True}))}

    assert server._idle_task is None
    await server._on_tool_call({"name": "test", "arguments": {}})
    assert server._idle_task is not None

    # 清理: cancel task 不影响别的测试
    server._stopping = True
    server._idle_task.cancel()
    try:
        await server._idle_task
    except (asyncio.CancelledError, Exception):
        pass


# ── _idle_watch loop ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idle_watch_exits_when_bridge_not_serial():
    """watcher 醒来发现 bridge 已经不是 SerialHidBridge (e.g. 被 stop) → 退出."""
    server = _mk_server(idle_close_after=0.1)
    server.bridge = MockBridge()  # 故意非 SerialHidBridge

    # 直接跑 _idle_watch, 应该一觉醒来检测到 bridge 非 serial → return
    task = asyncio.create_task(server._idle_watch())
    await asyncio.sleep(0.15)  # 让 watcher sleep + 醒来一次
    assert task.done()
    await task  # 不抛


@pytest.mark.asyncio
async def test_idle_watch_triggers_release_when_threshold_crossed():
    """sleep 醒来发现 idle 超阈值 → release + 退出 loop."""
    server = _mk_server(idle_close_after=0.1)
    fake_serial = AsyncMock(spec=SerialHidBridge)
    fake_serial.port = "COM6"
    fake_serial.close = AsyncMock()
    server.bridge = fake_serial
    # 模拟 last_used_at 是很久以前 (idle 已超阈值)
    server._last_used_at = time.monotonic() - 1.0

    task = asyncio.create_task(server._idle_watch())
    await asyncio.sleep(0.15)  # watcher sleep ~0.1s 醒来
    assert task.done()
    await task
    # release 已发生 — bridge 变 UnavailableBridge
    assert isinstance(server.bridge, UnavailableBridge)


@pytest.mark.asyncio
async def test_idle_watch_keeps_alive_while_active():
    """tool call 持续保持 _last_used_at 新 → watcher 不 release."""
    server = _mk_server(idle_close_after=0.3)
    fake_serial = AsyncMock(spec=SerialHidBridge)
    fake_serial.port = "COM6"
    fake_serial.close = AsyncMock()
    server.bridge = fake_serial
    server._last_used_at = time.monotonic()

    task = asyncio.create_task(server._idle_watch())
    # 不断"使用" mcp, last_used_at 一直新
    for _ in range(3):
        await asyncio.sleep(0.1)
        server._last_used_at = time.monotonic()
    # watcher 应该还在跑, bridge 还是 SerialHidBridge
    assert not task.done()
    assert server.bridge is fake_serial

    # 清理
    server._stopping = True
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ── stop() 清理 idle task ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_cancels_idle_task():
    server = _mk_server(idle_close_after=1.0)
    fake_serial = AsyncMock(spec=SerialHidBridge)
    fake_serial.port = "COM6"
    fake_serial.close = AsyncMock()
    server.bridge = fake_serial
    server.tools = {"test": MagicMock(handler=AsyncMock(return_value={"ok": True}))}
    await server._on_tool_call({"name": "test", "arguments": {}})
    assert server._idle_task is not None

    await server.stop()

    assert server._idle_task is None or server._idle_task.done()
    assert server._stopping is True
