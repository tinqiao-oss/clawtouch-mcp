"""USB HID keycode tables (US layout).

Split off from bridge.py so other frontends (e.g. OS-level automation) can reuse
the same name -> keycode mapping without pulling in pyserial.
"""
from __future__ import annotations


# Non-shifted printable characters
_CHAR_TO_HID: dict[str, int] = {}
for _i in range(26):
    _CHAR_TO_HID[chr(ord("a") + _i)] = 0x04 + _i
    _CHAR_TO_HID[chr(ord("A") + _i)] = 0x04 + _i
for _i in range(1, 10):
    _CHAR_TO_HID[str(_i)] = 0x1D + _i
_CHAR_TO_HID["0"] = 0x27
for _k, _v in {
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
    ";": 0x33, "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37,
    "/": 0x38, " ": 0x2C,
}.items():
    _CHAR_TO_HID[_k] = _v

# Shifted printable characters
_SHIFT_CHAR_TO_HID: dict[str, int] = {
    "!": 0x1E, "@": 0x1F, "#": 0x20, "$": 0x21, "%": 0x22,
    "^": 0x23, "&": 0x24, "*": 0x25, "(": 0x26, ")": 0x27,
    "_": 0x2D, "+": 0x2E, "{": 0x2F, "}": 0x30, "|": 0x31,
    ":": 0x33, '"': 0x34, "~": 0x35, "<": 0x36, ">": 0x37, "?": 0x38,
}
for _i in range(26):
    _SHIFT_CHAR_TO_HID[chr(ord("A") + _i)] = 0x04 + _i

# Named keys
_KEY_NAME_TO_HID: dict[str, int] = {
    "enter": 0x28, "return": 0x28,
    "escape": 0x29, "esc": 0x29,
    "backspace": 0x2A, "delete": 0x4C,
    "tab": 0x2B, "space": 0x2C,
    "up": 0x52, "down": 0x51, "left": 0x50, "right": 0x4F,
    "home": 0x4A, "end": 0x4D, "pageup": 0x4B, "pagedown": 0x4E,
    "insert": 0x49,
    # Punctuation aliases that show up in app shortcuts as worded names —
    # without these, `hid.key("ctrl+shift+plus")` would fail to parse.
    "plus": 0x2E, "equal": 0x2E, "equals": 0x2E,
    "minus": 0x2D, "hyphen": 0x2D, "dash": 0x2D,
    "comma": 0x36, "period": 0x37, "dot": 0x37,
    "slash": 0x38, "backslash": 0x31,
    "semicolon": 0x33, "apostrophe": 0x34, "quote": 0x34,
    "grave": 0x35, "backtick": 0x35, "tilde": 0x35,
    "leftbracket": 0x2F, "rightbracket": 0x30,
    **{f"f{i}": 0x3A + i - 1 for i in range(1, 13)},
}

# Worded names whose corresponding US-layout glyph is the SHIFTED form
# of the underlying HID keycode. ``hid.key("plus")`` must emit the
# physical key for ``=`` AND hold SHIFT — otherwise it would type ``=``
# instead of ``+``. Likewise ``tilde`` (shift+`) and ``quote`` (shift+').
# Everything else in ``_KEY_NAME_TO_HID`` (equal, minus, comma, ...) is
# the *non-shifted* glyph and must NOT have shift forced.
_SHIFTED_NAMES: frozenset[str] = frozenset({
    "plus",      # shift+= → '+'
    "tilde",     # shift+` → '~'
    "quote",     # shift+' → '"' (apostrophe is the un-shifted alias)
})


def char_needs_shift(ch: str) -> bool:
    return ch in _SHIFT_CHAR_TO_HID


def char_to_keycode(ch: str) -> int | None:
    # Prefer the shifted table (uppercase letters / shifted punctuation),
    # fall back to the non-shifted table. Use an explicit ``is None``
    # check rather than ``or`` so a future keycode value of 0 would not
    # be silently dropped.
    shifted = _SHIFT_CHAR_TO_HID.get(ch)
    if shifted is not None:
        return shifted
    return _CHAR_TO_HID.get(ch)


def name_to_keycode(name: str) -> int | None:
    return _KEY_NAME_TO_HID.get(name.strip().lower())


def name_needs_shift(name: str) -> bool:
    """Whether the named key represents the *shifted* form of its
    underlying HID keycode — callers that build a combo must OR the
    SHIFT modifier in when this is True.

    True for ``plus`` / ``tilde`` / ``quote`` (and their case
    variants). False for everything else, including ``equal`` /
    ``minus`` / ``apostrophe`` (which are the un-shifted glyphs of
    the same physical key) and all the navigation / function-key
    names (whose keycodes don't have a SHIFT interpretation at all).
    """
    return name.strip().lower() in _SHIFTED_NAMES
