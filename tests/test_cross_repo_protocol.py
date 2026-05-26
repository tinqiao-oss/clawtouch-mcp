"""Cross-repo wire-protocol byte-equality regression.

The v1.0 wire protocol lives in two independently maintained Python
packages:

  * ``clawtouch-mcp/clawtouch_mcp/protocol.py``  — bundled with the MCP
    server (this repo).
  * ``clawtouch-hid-protocol/clawtouch_hid_protocol/protocol.py`` — the
    host-side definitions shipped with the firmware repo.

Both serialise the *same frozen v1.0 frame format*, byte-for-byte —
that's the whole point of "frozen v1.0". A drift between the two
silently breaks every Pico that runs firmware built against one and
talks to a host built against the other. Nothing else catches this:
each repo has its own ``test_protocol.py`` that only validates its own
builder/parser symmetry.

These tests fire every public builder with the same arguments through
both packages and assert byte-equal ``.serialize()`` output, plus
enum-value equality on every shared constant.

The companion package is only installed when the developer runs
``pip install -e oss/clawtouch-hid`` (it's not a runtime dep of
``clawtouch-mcp``), so we skip the suite when it's absent rather
than fail the default test run.
"""
from __future__ import annotations

import pytest

ext = pytest.importorskip(
    "clawtouch_hid_protocol",
    reason="dev-only cross-repo check; "
           "install with `pip install -e oss/clawtouch-hid`",
)

# Import the local copy directly under a different name so we can
# compare the two modules side by side.
from clawtouch_mcp import protocol as mcp_proto  # noqa: E402

ext_proto = ext  # rename for readability below


# ── Constants ─────────────────────────────────────────────────────────


class TestSharedConstants:
    def test_frame_header(self):
        assert mcp_proto.FRAME_HEADER == ext_proto.FRAME_HEADER

    def test_max_payload_len(self):
        assert mcp_proto.MAX_PAYLOAD_LEN == ext_proto.MAX_PAYLOAD_LEN

    def test_protocol_version_string(self):
        assert mcp_proto.PROTOCOL_VERSION == ext_proto.PROTOCOL_VERSION


class TestEnumValueParity:
    """The Python class identities differ (separate module imports),
    but every named member must have the same integer value or a
    firmware that names commands by code will mis-dispatch."""

    def _values(self, enum_cls) -> dict[str, int]:
        return {m.name: int(m.value) for m in enum_cls}

    def test_command_type(self):
        assert self._values(mcp_proto.CommandType) == self._values(ext_proto.CommandType)

    def test_mouse_button(self):
        assert self._values(mcp_proto.MouseButton) == self._values(ext_proto.MouseButton)

    def test_modifier_key(self):
        assert self._values(mcp_proto.ModifierKey) == self._values(ext_proto.ModifierKey)

    def test_error_code(self):
        assert self._values(mcp_proto.ErrorCode) == self._values(ext_proto.ErrorCode)


# ── Builders: byte-equal frames ───────────────────────────────────────


def _both(mcp_call, ext_call):
    """Call both builders with the same args (closures), serialise,
    and assert byte-equal."""
    mcp_frame = mcp_call().serialize()
    ext_frame = ext_call().serialize()
    assert mcp_frame == ext_frame, (
        f"frame mismatch:\n  mcp={mcp_frame.hex()}\n  ext={ext_frame.hex()}"
    )


class TestBuilderByteEquality:
    def test_ping(self):
        _both(
            lambda: mcp_proto.build_ping(seq_id=42),
            lambda: ext_proto.build_ping(seq_id=42),
        )

    def test_mouse_move_relative(self):
        _both(
            lambda: mcp_proto.build_mouse_move(640, 360, relative=True, seq_id=7),
            lambda: ext_proto.build_mouse_move(640, 360, relative=True, seq_id=7),
        )

    def test_mouse_move_absolute_flag(self):
        # Even though v1.0 firmware ignores the flag (treats all as
        # relative), the wire byte must still be set consistently.
        _both(
            lambda: mcp_proto.build_mouse_move(100, 200, relative=False, seq_id=8),
            lambda: ext_proto.build_mouse_move(100, 200, relative=False, seq_id=8),
        )

    def test_mouse_move_negative_delta(self):
        _both(
            lambda: mcp_proto.build_mouse_move(-50, -30, relative=True, seq_id=9),
            lambda: ext_proto.build_mouse_move(-50, -30, relative=True, seq_id=9),
        )

    def test_mouse_click_left_single(self):
        _both(
            lambda: mcp_proto.build_mouse_click(mcp_proto.MouseButton.LEFT, double=False, seq_id=10),
            lambda: ext_proto.build_mouse_click(ext_proto.MouseButton.LEFT, double=False, seq_id=10),
        )

    def test_mouse_click_right_double(self):
        _both(
            lambda: mcp_proto.build_mouse_click(mcp_proto.MouseButton.RIGHT, double=True, seq_id=11),
            lambda: ext_proto.build_mouse_click(ext_proto.MouseButton.RIGHT, double=True, seq_id=11),
        )

    def test_mouse_scroll(self):
        _both(
            lambda: mcp_proto.build_mouse_scroll(5, seq_id=12),
            lambda: ext_proto.build_mouse_scroll(5, seq_id=12),
        )

    def test_key_press_with_modifiers(self):
        _both(
            lambda: mcp_proto.build_key_press(keycode=0x04, modifiers=int(mcp_proto.ModifierKey.CTRL), seq_id=13),
            lambda: ext_proto.build_key_press(keycode=0x04, modifiers=int(ext_proto.ModifierKey.CTRL), seq_id=13),
        )

    def test_key_release_all_zero(self):
        # The (0, 0) form documented in protocol-v1.md §3.3 as
        # "release-all" — locked-in v1.0 frame.
        _both(
            lambda: mcp_proto.build_key_release(seq_id=14),
            lambda: ext_proto.build_key_release(seq_id=14),
        )

    def test_key_combo_ctrl_c(self):
        ctrl = int(mcp_proto.ModifierKey.CTRL)
        _both(
            lambda: mcp_proto.build_key_combo(modifiers=ctrl, keycode=0x06, seq_id=15),
            lambda: ext_proto.build_key_combo(modifiers=ctrl, keycode=0x06, seq_id=15),
        )

    def test_key_combo_byte_order_locked(self):
        # KEY_PRESS is [keycode, modifiers]; KEY_COMBO is
        # [modifiers, keycode] (intentional historical asymmetry
        # documented in protocol-v1.md). If a future refactor swaps
        # either, this byte-equality assertion catches it.
        ctrl_shift = int(mcp_proto.ModifierKey.CTRL) | int(mcp_proto.ModifierKey.SHIFT)
        _both(
            lambda: mcp_proto.build_key_combo(modifiers=ctrl_shift, keycode=0x2E, seq_id=16),
            lambda: ext_proto.build_key_combo(modifiers=ctrl_shift, keycode=0x2E, seq_id=16),
        )

    def test_type_string_ascii(self):
        _both(
            lambda: mcp_proto.build_type_string("hello", seq_id=17),
            lambda: ext_proto.build_type_string("hello", seq_id=17),
        )

    def test_type_string_utf8_multibyte(self):
        # UTF-8 encoding for CJK / emoji must match between repos.
        _both(
            lambda: mcp_proto.build_type_string("你好", seq_id=18),
            lambda: ext_proto.build_type_string("你好", seq_id=18),
        )


class TestRoundTrip:
    """Frame built by one repo, deserialised by the other — covers the
    parser side as well as the builder side."""

    def test_mcp_built_frame_parses_in_ext(self):
        wire = mcp_proto.build_mouse_click(mcp_proto.MouseButton.LEFT, seq_id=99).serialize()
        parsed = ext_proto.HidCommand.deserialize(wire)
        assert int(parsed.cmd_type) == int(mcp_proto.CommandType.MOUSE_CLICK)
        assert parsed.seq_id == 99

    def test_ext_built_frame_parses_in_mcp(self):
        wire = ext_proto.build_mouse_click(ext_proto.MouseButton.LEFT, seq_id=99).serialize()
        parsed = mcp_proto.HidCommand.deserialize(wire)
        assert int(parsed.cmd_type) == int(ext_proto.CommandType.MOUSE_CLICK)
        assert parsed.seq_id == 99
