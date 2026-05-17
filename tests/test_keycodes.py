"""Keycode mapping smoke tests (US layout).

US layout is the lowest common denominator — hosts with a different
system input method may render typed characters as the wrong glyph. This
is documented behavior, not a bug. These tests only verify the mapping
itself, not OS-level behavior.
"""
from __future__ import annotations

from clawtouch_mcp.keycodes import (
    char_needs_shift,
    char_to_keycode,
    name_to_keycode,
)


class TestCharMapping:
    def test_lowercase_letters(self):
        for i in range(26):
            ch = chr(ord("a") + i)
            assert char_to_keycode(ch) == 0x04 + i

    def test_uppercase_letters_shift_required(self):
        for i in range(26):
            ch = chr(ord("A") + i)
            assert char_to_keycode(ch) == 0x04 + i
            assert char_needs_shift(ch) is True

    def test_digits(self):
        assert char_to_keycode("1") == 0x1E
        assert char_to_keycode("9") == 0x26
        assert char_to_keycode("0") == 0x27

    def test_space_and_common_punctuation(self):
        assert char_to_keycode(" ") == 0x2C
        assert char_to_keycode(".") == 0x37
        assert char_to_keycode(",") == 0x36
        assert char_to_keycode("/") == 0x38

    def test_shifted_punctuation_needs_shift(self):
        for ch in "!@#$%^&*()_+{}|:\"<>?~":
            assert char_needs_shift(ch) is True, f"{ch!r} should need shift"
            assert char_to_keycode(ch) is not None

    def test_unmapped_char_returns_none(self):
        # Chinese characters are not in US layout, so they have no keycode.
        # `hid.type` will need a different fallback (Unicode input mode)
        # if we ever support them — for now, type() will skip silently.
        assert char_to_keycode("中") is None


class TestNamedKeys:
    def test_navigation(self):
        assert name_to_keycode("up") == 0x52
        assert name_to_keycode("down") == 0x51
        assert name_to_keycode("left") == 0x50
        assert name_to_keycode("right") == 0x4F

    def test_function_keys_f1_to_f12(self):
        for i in range(1, 13):
            assert name_to_keycode(f"f{i}") == 0x3A + i - 1

    def test_aliases(self):
        assert name_to_keycode("enter") == name_to_keycode("return")
        assert name_to_keycode("escape") == name_to_keycode("esc")

    def test_case_insensitive_and_trimmed(self):
        assert name_to_keycode("  ENTER  ") == 0x28

    def test_unknown_name_returns_none(self):
        assert name_to_keycode("turbofunction") is None
