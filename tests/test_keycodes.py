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
    name_needs_shift,
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

    def test_punctuation_aliases_keycode(self):
        # Worded aliases for punctuation keys (added in v0.2.3 codex
        # round 3 #4) — keycode is the physical key, the SHIFT
        # interpretation is carried separately by name_needs_shift().
        for alias, expected in [
            ("plus", 0x2E), ("equal", 0x2E), ("equals", 0x2E),
            ("minus", 0x2D), ("hyphen", 0x2D), ("dash", 0x2D),
            ("comma", 0x36), ("period", 0x37), ("dot", 0x37),
            ("slash", 0x38), ("backslash", 0x31),
            ("semicolon", 0x33), ("apostrophe", 0x34), ("quote", 0x34),
            ("grave", 0x35), ("backtick", 0x35), ("tilde", 0x35),
            ("leftbracket", 0x2F), ("rightbracket", 0x30),
        ]:
            assert name_to_keycode(alias) == expected, alias


class TestNameNeedsShift:
    """Worded aliases that name the *shifted* glyph must report
    needs_shift=True so callers (bridge.key_combo) can OR the SHIFT
    modifier in. The non-shifted aliases must NOT — adding SHIFT to
    `ctrl+equal` would silently change it to `ctrl++` (= shift+=)."""

    def test_shifted_aliases_need_shift(self):
        for alias in ["plus", "tilde", "quote"]:
            assert name_needs_shift(alias) is True, alias
            # case-insensitive + whitespace tolerant
            assert name_needs_shift(alias.upper()) is True
            assert name_needs_shift(f"  {alias}  ") is True

    def test_unshifted_aliases_do_not_need_shift(self):
        for alias in [
            "equal", "equals", "minus", "hyphen", "dash",
            "comma", "period", "dot",
            "slash", "backslash",
            "semicolon", "apostrophe",
            "grave", "backtick",
            "leftbracket", "rightbracket",
            # navigation / function keys never need shift via this path
            "enter", "tab", "escape", "f1", "f12",
        ]:
            assert name_needs_shift(alias) is False, alias

    def test_unknown_name_does_not_claim_shift(self):
        assert name_needs_shift("totally-fake-key") is False


class TestCharToKeycodeFallback:
    """char_to_keycode now uses an explicit ``is None`` check rather
    than ``or`` — guards against a future keycode value of 0 being
    silently dropped. All current HID keycodes start at 0x04 so the
    old code worked by accident, but the new code is explicit."""

    def test_shifted_takes_precedence_over_unshifted(self):
        # 'A' is in both tables (both at 0x04) — shifted entry wins.
        # Equality holds because they map to the same keycode, but
        # the lookup ordering is now explicit and stable.
        assert char_to_keycode("A") == 0x04

    def test_unshifted_when_shifted_absent(self):
        assert char_to_keycode("a") == 0x04
        assert char_to_keycode("1") == 0x1E
        assert char_to_keycode(" ") == 0x2C
