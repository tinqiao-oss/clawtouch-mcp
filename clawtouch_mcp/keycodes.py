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
    **{f"f{i}": 0x3A + i - 1 for i in range(1, 13)},
}


def char_needs_shift(ch: str) -> bool:
    return ch in _SHIFT_CHAR_TO_HID


def char_to_keycode(ch: str) -> int | None:
    return _SHIFT_CHAR_TO_HID.get(ch) or _CHAR_TO_HID.get(ch)


def name_to_keycode(name: str) -> int | None:
    return _KEY_NAME_TO_HID.get(name.strip().lower())
