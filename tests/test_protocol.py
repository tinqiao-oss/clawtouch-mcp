# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Wire protocol smoke tests — round-trip + builders + modifier mask.

These tests lock the v1.0 frame layout. If they break, the firmware will
stop talking to this MCP server. Do not "fix" a failure here by changing
the test — fix the protocol module instead, and bump the spec in the
companion `clawtouch-hid` repository at the same time.
"""
from __future__ import annotations

import pytest

from clawtouch_mcp.protocol import (
    FRAME_HEADER,
    MAX_PAYLOAD_LEN,
    CommandType,
    HidCommand,
    ModifierKey,
    MouseButton,
    ProtocolError,
    build_key_combo,
    build_key_press,
    build_key_release,
    build_mouse_click,
    build_mouse_move,
    build_mouse_scroll,
    build_ping,
    build_type_string,
    modifiers_to_mask,
)


class TestFrameRoundtrip:
    def test_ping_roundtrip(self):
        original = build_ping(seq_id=42)
        wire = original.serialize()
        decoded = HidCommand.deserialize(wire)
        assert decoded.cmd_type == CommandType.PING
        assert decoded.seq_id == 42
        assert decoded.payload == b""

    def test_mouse_move_absolute(self):
        original = build_mouse_move(500, 300, relative=False, seq_id=7)
        wire = original.serialize()
        # Header check
        assert wire[0] == FRAME_HEADER
        # Roundtrip
        decoded = HidCommand.deserialize(wire)
        assert decoded.cmd_type == CommandType.MOUSE_MOVE
        assert decoded.seq_id == 7
        # Payload: x(2) y(2) flags(1)
        assert len(decoded.payload) == 5
        assert decoded.payload[4] == 0x00  # not relative

    def test_mouse_move_relative_flag_set(self):
        wire = build_mouse_move(10, 20, relative=True).serialize()
        decoded = HidCommand.deserialize(wire)
        assert decoded.payload[4] == 0x01

    def test_mouse_click_payload(self):
        wire = build_mouse_click(MouseButton.RIGHT, double=True).serialize()
        decoded = HidCommand.deserialize(wire)
        assert decoded.payload[0] == int(MouseButton.RIGHT)
        assert decoded.payload[1] == 0x01  # double flag

    def test_type_string_utf8(self):
        wire = build_type_string("hello 你好").serialize()
        decoded = HidCommand.deserialize(wire)
        assert decoded.cmd_type == CommandType.KEY_TYPE_STRING
        assert decoded.payload.decode("utf-8") == "hello 你好"

    def test_key_combo_byte_order(self):
        """KEY_COMBO is [modifiers, keycode] — same as KEY_PRESS/KEY_RELEASE (unified v1.1.1).

        Any reorder will silently break the firmware.
        """
        wire = build_key_combo(int(ModifierKey.CTRL) | int(ModifierKey.SHIFT),
                                0x04, seq_id=1).serialize()
        decoded = HidCommand.deserialize(wire)
        assert decoded.payload[0] == 0x03  # CTRL|SHIFT
        assert decoded.payload[1] == 0x04

    def test_key_press_byte_order(self):
        """KEY_PRESS is [modifiers, keycode] — unified with KEY_RELEASE/KEY_COMBO (v1.1.1)."""
        wire = build_key_press(0x05, int(ModifierKey.ALT), seq_id=1).serialize()
        decoded = HidCommand.deserialize(wire)
        assert decoded.payload[0] == 0x04  # modifiers first (ALT)
        assert decoded.payload[1] == 0x05  # keycode

    def test_key_release_all_payload(self):
        """release-all is [0x00, 0x00] — firmware rejects empty payload with
        ERR_INVALID_PAYLOAD (handler requires len(payload) >= 2)."""
        wire = build_key_release().serialize()
        decoded = HidCommand.deserialize(wire)
        assert decoded.cmd_type == CommandType.KEY_RELEASE
        assert decoded.payload == b"\x00\x00"

    def test_key_release_specific(self):
        """KEY_RELEASE byte order is [modifiers, keycode] — same as KEY_PRESS/KEY_COMBO (v1.1.1)."""
        wire = build_key_release(0x05, int(ModifierKey.ALT), seq_id=2).serialize()
        decoded = HidCommand.deserialize(wire)
        assert decoded.seq_id == 2
        assert decoded.payload[0] == 0x04  # modifiers first (ALT)
        assert decoded.payload[1] == 0x05  # keycode

    def test_scroll_signed_delta(self):
        """delta is signed int16 — negative scroll up."""
        wire = build_mouse_scroll(-50).serialize()
        decoded = HidCommand.deserialize(wire)
        import struct
        delta = struct.unpack("<h", decoded.payload)[0]
        assert delta == -50

    def test_seq_id_wraps_at_u16(self):
        """seq_id outside u16 range is masked, not rejected."""
        wire = build_ping(seq_id=0x1_0001).serialize()
        decoded = HidCommand.deserialize(wire)
        assert decoded.seq_id == 0x0001  # & 0xFFFF


class TestChecksum:
    def test_corrupted_checksum_raises(self):
        wire = bytearray(build_ping(seq_id=1).serialize())
        wire[-1] ^= 0xFF  # flip last byte = checksum
        with pytest.raises(ProtocolError, match="checksum"):
            HidCommand.deserialize(bytes(wire))

    def test_short_frame_raises(self):
        with pytest.raises(ProtocolError):
            HidCommand.deserialize(b"\xaa\x00\x00")  # 3 bytes < 7 minimum

    def test_bad_header_raises(self):
        wire = bytearray(build_ping().serialize())
        wire[0] = 0x55  # not 0xAA
        with pytest.raises(ProtocolError):
            HidCommand.deserialize(bytes(wire))

    def test_unknown_command_raises(self):
        # Manually craft a frame with bogus cmd_type 0x99
        import struct
        seq = struct.pack("<H", 0)
        plen = struct.pack("<H", 0)
        data = bytes([FRAME_HEADER]) + seq + bytes([0x99]) + plen
        csum = sum(data) & 0xFF
        with pytest.raises(ProtocolError, match="unknown command"):
            HidCommand.deserialize(data + bytes([csum]))


class TestPayloadCap:
    def test_oversize_payload_rejected(self):
        oversized = HidCommand(CommandType.KEY_TYPE_STRING,
                                payload=b"x" * (MAX_PAYLOAD_LEN + 1))
        with pytest.raises(ProtocolError, match="too large"):
            oversized.serialize()

    def test_exact_max_payload_ok(self):
        max_cmd = HidCommand(CommandType.KEY_TYPE_STRING,
                              payload=b"x" * MAX_PAYLOAD_LEN)
        wire = max_cmd.serialize()
        decoded = HidCommand.deserialize(wire)
        assert len(decoded.payload) == MAX_PAYLOAD_LEN


class TestModifierMapping:
    def test_all_aliases(self):
        """ctrl/control / win/cmd/gui aliases all map to canonical modifiers."""
        assert modifiers_to_mask(["ctrl"]) == int(ModifierKey.CTRL)
        assert modifiers_to_mask(["control"]) == int(ModifierKey.CTRL)
        assert modifiers_to_mask(["cmd"]) == int(ModifierKey.GUI)
        assert modifiers_to_mask(["win"]) == int(ModifierKey.GUI)
        assert modifiers_to_mask(["gui"]) == int(ModifierKey.GUI)

    def test_combined_mask(self):
        mask = modifiers_to_mask(["ctrl", "shift", "alt"])
        assert mask == int(ModifierKey.CTRL) | int(ModifierKey.SHIFT) | int(ModifierKey.ALT)

    def test_case_insensitive_and_trimmed(self):
        assert modifiers_to_mask(["  CTRL  ", "Shift"]) == 0x03

    def test_unknown_modifier_raises(self):
        with pytest.raises(ProtocolError, match="unknown modifier"):
            modifiers_to_mask(["super"])
