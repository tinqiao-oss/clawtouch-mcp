# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""End-to-end byte-order lock for the real SerialHidBridge keyboard path.

The v1.1.1 keyboard byte-order unification (`[modifiers, keycode]`) is the
highest-stakes recent wire change, yet the server-level tests drive a
MockBridge that only records calls — it never builds a frame. So a
regression that swapped `bridge.key_press`'s positional arguments to
`build_key_press(mask, kc)` would pass the entire server suite while
emitting the wrong key on real hardware.

These tests stand up a real ``SerialHidBridge`` (no hardware — ``_send_raw``
is stubbed, the constructor opens nothing) and capture the ``HidCommand``
the bridge builds, asserting the serialized payload is ``[modifiers,
keycode]``.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from clawtouch_mcp.bridge import SerialHidBridge
from clawtouch_mcp.protocol import CommandType


def _run_and_capture(call):
    """Run ``call(bridge)`` with ``_send_raw`` stubbed; return the built
    HidCommand and the method's boolean result."""
    bridge = SerialHidBridge("/dev/null", baudrate=115200)
    captured = {}

    async def fake_send_raw(cmd):
        captured["cmd"] = cmd
        return SimpleNamespace(cmd_type=CommandType.ACK, seq_id=cmd.seq_id)

    bridge._send_raw = fake_send_raw
    ok = asyncio.run(call(bridge))
    return captured.get("cmd"), ok


class TestSerialBridgeKeyboardByteOrder:
    def test_key_press_payload_is_modifiers_then_keycode(self):
        cmd, ok = _run_and_capture(lambda b: b.key_press("a", ["ctrl"]))
        assert ok is True
        assert cmd.cmd_type == CommandType.KEY_PRESS
        # 'a' = 0x04, ctrl = 0x01; wire payload MUST be [modifiers, keycode].
        # If bridge.key_press were build_key_press(mask, kc) this would be
        # b"\x04\x01" and fail.
        assert cmd.payload == bytes([0x01, 0x04])

    def test_key_release_specific_payload_is_modifiers_then_keycode(self):
        cmd, ok = _run_and_capture(lambda b: b.key_release("a", ["ctrl"]))
        assert ok is True
        assert cmd.cmd_type == CommandType.KEY_RELEASE
        assert cmd.payload == bytes([0x01, 0x04])

    def test_key_release_all_zero_is_panic_stop(self):
        cmd, ok = _run_and_capture(lambda b: b.key_release())
        assert ok is True
        assert cmd.cmd_type == CommandType.KEY_RELEASE
        assert cmd.payload == bytes([0x00, 0x00])
