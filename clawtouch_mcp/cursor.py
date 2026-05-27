"""Cross-platform OS cursor position query for absolute-coordinate support.

The ClawTouch HID firmware is a USB Boot Mouse: it can only emit
**relative** pixel deltas (the USB HID Mouse class itself has no notion
of absolute coordinates — only a wider HID Digitizer / Tablet PC
profile would, and we don't ship that). To support an absolute-
coordinate API like `hid.click(x=640, y=360)` from the host side, we
have to:

  1. Ask the host OS where the cursor currently is.
  2. Compute ``(dx, dy) = (target_x - current_x, target_y - current_y)``.
  3. Send the firmware a *relative* move of ``(dx, dy)``.

This module provides step 1. It is intentionally dependency-free —
each platform path uses only ``ctypes`` against the OS's system
libraries (``user32.dll`` on Windows, ``CoreGraphics.framework`` on
macOS, ``libX11.so`` on Linux/X11). Every code path is wrapped in a
broad ``try`` and returns ``None`` on failure so the caller can fall
back to an explicit error without crashing the MCP server.

Wayland is not supported — there is no public unprivileged API to
query cursor position under Wayland, by design. Users on Wayland get
``None`` and the MCP server returns a clear error message asking them
to call ``hid.move(..., relative=True)`` explicitly.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform


_FAKE_CURSOR_ENV = "CLAWTOUCH_FAKE_CURSOR"

# Mock-bridge cursor state. ``MockBridge.mouse_move`` seeds + mutates
# this list so that the closed-loop converge path in the server sees
# the cursor "actually" land where it told the firmware to go. This
# is mock infrastructure, not a test hack — a hardware mock that
# doesn't simulate cursor reflux would mis-model the very feedback
# loop we're trying to exercise. Production code (SerialHidBridge)
# never touches these helpers; ``get_cursor_position`` only consults
# the dynamic state when MockBridge has seeded it.
_FAKE_DYNAMIC_STATE: list[int] | None = None


def _seed_fake_cursor(x: int, y: int) -> None:
    """Initialize the mock-bridge cursor state. Called once by
    ``MockBridge.__init__``."""
    global _FAKE_DYNAMIC_STATE
    _FAKE_DYNAMIC_STATE = [int(x), int(y)]


def _update_fake_cursor(dx: int, dy: int, *, relative: bool = True) -> None:
    """Apply a firmware-emitted delta to the mock cursor state.
    No-op when the dynamic state hasn't been seeded (production /
    real-bridge code path)."""
    if _FAKE_DYNAMIC_STATE is None:
        return
    if relative:
        _FAKE_DYNAMIC_STATE[0] += int(dx)
        _FAKE_DYNAMIC_STATE[1] += int(dy)
    else:
        _FAKE_DYNAMIC_STATE[0] = int(dx)
        _FAKE_DYNAMIC_STATE[1] = int(dy)


def _clear_fake_cursor() -> None:
    """Reset the dynamic state — used by tests that need to exercise
    the cursor-unavailable error path."""
    global _FAKE_DYNAMIC_STATE
    _FAKE_DYNAMIC_STATE = None


def get_cursor_position() -> tuple[int, int] | None:
    """Return the current OS cursor position as (x, y) in physical
    pixels, or ``None`` if the platform path is unavailable or any
    OS call fails.

    Coordinate space matches what the platform's input APIs report:
    on Windows, this is the primary monitor's physical-pixel
    coordinate space (matching what ``--screen WxH`` auto-detects).
    On macOS, it's the main display's point coordinate space (note:
    not pixel — Retina displays scale 2:1; callers that pass
    ``--screen`` in pixel terms need to be aware of the mismatch).
    On Linux/X11, it's the root window pixel space.

    Test hook: setting the ``CLAWTOUCH_FAKE_CURSOR=x,y`` environment
    variable bypasses the OS query and returns the parsed (x, y) tuple
    instead. Headless CI uses this to exercise the absolute-coordinate
    code path deterministically. Parse failures fall through to the
    real OS query path so a malformed value never silently breaks
    the production code path.

    Never raises — broad exception catches return ``None``.
    """
    # Mock-bridge dynamic state wins — seeded by MockBridge.__init__
    # in ``--mock`` mode so the closed-loop converge path lands.
    if _FAKE_DYNAMIC_STATE is not None:
        return (_FAKE_DYNAMIC_STATE[0], _FAKE_DYNAMIC_STATE[1])

    fake = os.environ.get(_FAKE_CURSOR_ENV)
    if fake:
        try:
            sx, sy = fake.split(",", 1)
            return (int(sx.strip()), int(sy.strip()))
        except (ValueError, AttributeError):
            pass  # malformed → fall through to real OS query

    system = platform.system()
    if system == "Windows":
        return _windows_get_cursor()
    if system == "Darwin":
        return _macos_get_cursor()
    if system == "Linux":
        return _linux_get_cursor()
    return None


# ── Windows ───────────────────────────────────────────────────────────


class _Win32Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _windows_get_cursor() -> tuple[int, int] | None:
    try:
        # GetCursorPos returns physical coordinates iff the calling
        # process is per-monitor DPI aware. server.py calls
        # SetProcessDpiAwareness(2) for the screen-auto-detect path;
        # if that ran first we get physical pixels here too.
        user32 = ctypes.windll.user32
        point = _Win32Point()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        return (int(point.x), int(point.y))
    except Exception:
        return None


# ── macOS (CoreGraphics via ctypes, no pyobjc dep) ────────────────────


class _CGPoint(ctypes.Structure):
    # CGFloat is 64-bit on every 64-bit macOS Python build we'd ship.
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _macos_get_cursor() -> tuple[int, int] | None:
    try:
        cg_path = ctypes.util.find_library("CoreGraphics") or \
            "/System/Library/Frameworks/ApplicationServices.framework/Versions/A/Frameworks/CoreGraphics.framework/CoreGraphics"
        cg = ctypes.cdll.LoadLibrary(cg_path)

        # CGEventRef CGEventCreate(CGEventSourceRef source);
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventCreate.argtypes = [ctypes.c_void_p]

        # CGPoint CGEventGetLocation(CGEventRef event);
        cg.CGEventGetLocation.restype = _CGPoint
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]

        # CFRelease — needed to avoid leaking the CGEventRef.
        cf_path = ctypes.util.find_library("CoreFoundation") or \
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        cf = ctypes.cdll.LoadLibrary(cf_path)
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        event = cg.CGEventCreate(None)
        if not event:
            return None
        try:
            point = cg.CGEventGetLocation(event)
            return (int(point.x), int(point.y))
        finally:
            cf.CFRelease(event)
    except Exception:
        return None


# ── Linux X11 (no Wayland support — by design) ────────────────────────


def _linux_get_cursor() -> tuple[int, int] | None:
    # Wayland short-circuit: WAYLAND_DISPLAY is the canonical signal.
    # On a Wayland-only session there's no unprivileged API to query
    # the cursor; fail soft and let the MCP server return a clear
    # error to the agent.
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        return None

    try:
        x11_path = ctypes.util.find_library("X11")
        if not x11_path:
            return None
        x11 = ctypes.cdll.LoadLibrary(x11_path)

        # Display *XOpenDisplay(_Xconst char *display_name);
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]

        # Window XDefaultRootWindow(Display *display);
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]

        # Bool XQueryPointer(Display *display, Window w,
        #                    Window *root_return, Window *child_return,
        #                    int *root_x_return, int *root_y_return,
        #                    int *win_x_return, int *win_y_return,
        #                    unsigned int *mask_return);
        x11.XQueryPointer.restype = ctypes.c_int
        x11.XQueryPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]

        display = x11.XOpenDisplay(None)
        if not display:
            return None
        try:
            root = x11.XDefaultRootWindow(display)
            root_return = ctypes.c_ulong()
            child_return = ctypes.c_ulong()
            root_x = ctypes.c_int()
            root_y = ctypes.c_int()
            win_x = ctypes.c_int()
            win_y = ctypes.c_int()
            mask = ctypes.c_uint()
            ok = x11.XQueryPointer(
                display, root,
                ctypes.byref(root_return), ctypes.byref(child_return),
                ctypes.byref(root_x), ctypes.byref(root_y),
                ctypes.byref(win_x), ctypes.byref(win_y),
                ctypes.byref(mask),
            )
            if not ok:
                return None
            return (int(root_x.value), int(root_y.value))
        finally:
            x11.XCloseDisplay(display)
    except Exception:
        return None


def availability_hint() -> str:
    """Human-readable explanation of what cursor tracking does on
    this platform — used in tool error messages when
    ``get_cursor_position()`` returns ``None``."""
    system = platform.system()
    if system == "Windows":
        return ("Windows cursor tracking uses user32.GetCursorPos via ctypes "
                "and should always succeed; if it returned None the OS call "
                "itself failed.")
    if system == "Darwin":
        return ("macOS cursor tracking uses CoreGraphics.CGEventGetLocation "
                "via ctypes; if it returned None CoreGraphics was unloadable "
                "(very unusual on a standard macOS Python build).")
    if system == "Linux":
        if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
            return ("Linux/Wayland session detected. Wayland has no "
                    "unprivileged API to query the cursor position, so "
                    "absolute-coordinate clicks are not supported. "
                    "Either start an X11 session, or call "
                    "`hid.move(x, y, relative=true)` and "
                    "`hid.click(x, y, relative=true)` directly with your "
                    "own deltas.")
        return ("Linux/X11 cursor tracking uses libX11.XQueryPointer via "
                "ctypes; if it returned None libX11 was unloadable or "
                "the X display was unreachable (check $DISPLAY).")
    return f"Cursor tracking is not implemented on platform {system!r}."
