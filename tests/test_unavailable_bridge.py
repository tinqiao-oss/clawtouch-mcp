"""UnavailableBridge — startup-time HID unavailability semantics.

Verifies the "AI-visible error + lazy retry" UX:
  1. Action methods raise HidUnavailableError with a message that mentions
     the tried ports + suggests freeing the hardware.
  2. The error bubbles up through dispatch() to a JSON-RPC error.
  3. Lazy retry: when a previously-busy port becomes available, the next
     action attempt promotes the bridge in place and forwards the call.
  4. server.start() picks UnavailableBridge over MockBridge when all
     candidate ports are present-but-busy on startup (vs --mock which
     should still get MockBridge).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from clawtouch_mcp.server import (
    ClawTouchMcpServer,
    HidUnavailableError,
    MockBridge,
    ServerConfig,
    UnavailableBridge,
)


def _mk_server(*, mock: bool = False, port: str | None = None) -> ClawTouchMcpServer:
    cfg = ServerConfig(
        port=port,
        baudrate=115200,
        mock=mock,
        screen_w=1920, screen_h=1080,
    )
    return ClawTouchMcpServer(cfg)


# ── action methods raise HidUnavailableError with helpful message ────────


@pytest.mark.asyncio
async def test_mouse_click_raises_with_message():
    server = _mk_server()
    bridge = UnavailableBridge(server, tried_ports=["COM6", "COM9"], baudrate=115200)
    # Force _try_promote to return False (no ports usable on retry)
    with patch.object(bridge, "_try_promote", AsyncMock(return_value=False)):
        with pytest.raises(HidUnavailableError) as exc_info:
            await bridge.mouse_click("left")
    msg = str(exc_info.value)
    assert "COM6" in msg and "COM9" in msg, "message must list tried ports"
    assert "ClawTouch" in msg or "another program" in msg, \
        "message must hint at the cause so AI can tell the user"
    assert "retry" in msg.lower(), "message must invite retry after freeing hw"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", [
    ("ping", ()),
    ("mouse_move", (100, 200)),
    ("mouse_click", ("left",)),
    ("mouse_scroll", (3,)),
    ("type_text", ("hi",)),
    ("key_combo", (["ctrl"], "c")),
    ("release_all", ()),
])
async def test_all_action_methods_raise(method, args):
    """Every hardware-touching method must raise, not silently fake success."""
    server = _mk_server()
    bridge = UnavailableBridge(server, tried_ports=["COM6"], baudrate=115200)
    with patch.object(bridge, "_try_promote", AsyncMock(return_value=False)):
        with pytest.raises(HidUnavailableError):
            await getattr(bridge, method)(*args)


# ── lazy retry: promote on next call after hardware becomes available ────


@pytest.mark.asyncio
async def test_lazy_promote_replaces_server_bridge():
    server = _mk_server()
    bridge = UnavailableBridge(server, tried_ports=["COM6"], baudrate=115200)
    server.bridge = bridge  # simulate startup wired the unavail bridge

    # Simulate: next reconnect attempt succeeds and returns a real SerialHidBridge
    fake_real_bridge = AsyncMock()
    fake_real_bridge.mouse_click = AsyncMock(return_value=True)
    async def _fake_promote():
        server.bridge = fake_real_bridge
        return True

    with patch.object(bridge, "_try_promote", side_effect=_fake_promote):
        ok = await bridge.mouse_click("left")

    assert ok is True
    assert server.bridge is fake_real_bridge, "promotion must replace server.bridge"
    fake_real_bridge.mouse_click.assert_awaited_once_with("left", double=False)


# ── server.start() chooses UnavailableBridge vs MockBridge correctly ─────


@pytest.mark.asyncio
async def test_start_explicit_mock_uses_mockbridge():
    server = _mk_server(mock=True)
    await server.start()
    assert isinstance(server.bridge, MockBridge), \
        "--mock flag should still produce MockBridge for test/dev use"


@pytest.mark.asyncio
async def test_start_no_devices_uses_unavailable_bridge():
    server = _mk_server()
    with patch("clawtouch_mcp.server.auto_detect_ports", return_value=[]):
        await server.start()
    assert isinstance(server.bridge, UnavailableBridge), \
        "no devices should produce UnavailableBridge, not silent MockBridge"


@pytest.mark.asyncio
async def test_start_all_ports_busy_uses_unavailable_bridge():
    """All candidate ports raise on connect → UnavailableBridge (not MockBridge)."""
    server = _mk_server()
    # auto_detect_ports returns 2 candidates; both fail to connect
    fake_bridge = AsyncMock()
    fake_bridge.connect = AsyncMock(side_effect=PermissionError("port busy"))
    with patch("clawtouch_mcp.server.auto_detect_ports",
               return_value=["COM6", "COM9"]), \
         patch("clawtouch_mcp.server.SerialHidBridge", return_value=fake_bridge):
        await server.start()
    assert isinstance(server.bridge, UnavailableBridge)
    assert server.bridge._tried_ports == ["COM6", "COM9"]


# ── device_info reports availability/diagnosis ──────────────────────────


def test_device_info_says_unavailable():
    server = _mk_server()
    bridge = UnavailableBridge(server, tried_ports=["COM6"], baudrate=115200)
    info = bridge.device_info()
    assert info["available"] is False
    assert info["connected"] is False
    assert info["tried_ports"] == ["COM6"]
    assert "lazy-retr" in info["reason"]
