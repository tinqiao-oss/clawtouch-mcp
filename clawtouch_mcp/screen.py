"""Screen-side helpers: top-level window geometry and calibration markers.

Two capabilities that are *about the screen*, not about the HID device,
kept here so ``server.py`` stays a protocol/tool layer and so the
OS-specific parts sit behind one import that is allowed to fail.

**Why a raise point, and why it has to be the caption.** A window that is
behind another one has to come forward before anything can be clicked in
it, and the way to do that without a focus-stealing API is to click it —
the same thing a person does, through the same physical mouse. But
clicking "any visible part of it" can press whatever happens to be
showing. So the point handed out is one the application itself calls a
drag area (``WM_NCHITTEST`` answering ``HTCAPTION``), and guessing "the
top strip is the title bar" is not good enough — in a browser that
strip is the tabs.

That answer narrows it down but does not settle it: ``HTCAPTION`` is what
the application says about dragging, not a promise that a click does
nothing. Chrome's "new tab" button answers ``HTCAPTION`` (measured), and
clicking it opens a tab. So the strip is scanned from the right, where
the space just inside the window controls is least likely to be
anything other than drag area,
rather than from the left, where the buttons are. It is best effort, and
the caller re-reads the window afterwards instead of trusting it.

**Why "visible" is not the same as "reachable".** A window can be
entirely unobstructed and still refuse every click, because a modal dialog
somewhere else has disabled it. Nothing about the pixels says so — the
window looks perfectly normal and a screenshot of it is perfectly valid —
yet input goes nowhere, physical mouse included. Windows says this plainly
through ``IsWindowEnabled``, so each window reports it.

**Why occlusion is measured, not assumed.** A screen capture of a
window's rectangle returns whatever is on top of that rectangle, which is
not the same thing as the window. Point a vision model at a covered
window and it faithfully describes the app sitting in front of it — a
confident, coherent, completely wrong answer that nothing downstream can
tell from a right one. So each window reports how much of it is actually
on top, and callers can refuse rather than look at the wrong pixels.

**Why window geometry lives in this server at all.** A vision model asked
to point at a UI element is only as accurate as the pixels it is given.
Handing it a 5120x1440 ultrawide desktop downscaled to fit a model's
input budget throws away exactly the detail the click depends on — in
practice the difference between "every click lands" and "every click
misses". Cropping to the target window first is what makes the rest
work, and cropping needs the window rectangle. The alternative — asking
the *agent* to guess a region — puts a number the agent cannot know into
the loop.

**Why calibration markers.** Every vision model rescales its input to an
internal resolution it does not report, so coordinates it returns are in
an unknown space. Drawing a shape whose position in the image is known
exactly, then asking the model where that shape is, measures the unknown
scale in the same call that asks about the target. Unlike picking a
known UI element as the anchor, this works on surfaces that expose no
accessibility tree at all (Electron, games, remote-desktop sessions) —
which are precisely the surfaces that need visual clicking.

Everything here is stdlib-only: ``ctypes`` on Windows, an optional
``Quartz`` import on macOS, and plain byte arithmetic for the markers.
No new install-time dependency, and every entry point degrades to a
clear "unsupported on this platform" rather than raising.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Optional

# ─────────────────────────── window listing ───────────────────────────


# Shell-owned windows that are visible and titled but are never a click
# target: the desktop, its wallpaper worker, and the taskbar.
_WIN32_SHELL_CLASSES = frozenset({
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
})

# Total time the whole window enumeration may spend hit-testing for raise
# points. A single unresponsive application can absorb the full per-probe
# timeout sixteen times over; a handful of them would push the enumeration
# past the caller's own timeout, and the caller's fallback is a full-screen
# capture — the one region this feature exists to avoid. Three seconds is
# far above the normal cost (a few milliseconds for a whole desktop) and
# far below any caller timeout.
RAISE_PROBE_BUDGET_S = 3.0
# ...and the most any single window may take out of it. Without this, one
# unresponsive application first in Z-order absorbs the whole allowance and
# every window behind it silently reports no raise point — the budget
# would be protecting the caller's timeout while quietly disabling the
# feature for everything else.
RAISE_PROBE_PER_WINDOW_S = 0.6


class WindowInfoUnavailable(RuntimeError):
    """Raised when this platform has no window-geometry implementation."""


def windows_supported() -> bool:
    """True when :func:`list_windows` can return real data here."""
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        try:
            import Quartz  # type: ignore # noqa: F401
        except Exception:
            return False
        return True
    return False


def unsupported_hint() -> str:
    if sys.platform == "darwin":
        return (
            "macOS window listing needs pyobjc: pip install "
            "'clawtouch-mcp[window]' (pyobjc-framework-Quartz). It also "
            "needs Screen Recording permission — without it macOS returns "
            "window rectangles but blanks the titles."
        )
    if sys.platform == "win32":  # pragma: no cover - always supported
        return ""
    return (
        "Window listing is implemented on Windows and macOS only. On "
        "Linux, pass an explicit `region` to hid.screenshot instead."
    )


def list_windows(include_offscreen: bool = False) -> list[dict[str, Any]]:
    """Visible top-level windows, front-most first where the OS tells us.

    Each entry: ``{"title", "pid", "rect": [x1, y1, x2, y2], "app",
    "foreground"}`` with ``rect`` in the same screen-pixel space that
    ``hid.click`` and ``hid.screenshot``'s ``region`` use.

    ``include_offscreen`` keeps minimized / fully off-screen windows,
    which are useless as screenshot regions but useful for answering
    "is this app even running".
    """
    if sys.platform == "win32":
        return _list_windows_win32(include_offscreen)
    if sys.platform == "darwin":
        return _list_windows_darwin(include_offscreen)
    raise WindowInfoUnavailable(unsupported_hint())


def _list_windows_win32(include_offscreen: bool) -> list[dict[str, Any]]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    dwmapi = None
    try:
        dwmapi = ctypes.windll.dwmapi
    except Exception:  # pragma: no cover - dwmapi missing pre-Vista only
        dwmapi = None

    # DWM window attributes. EXTENDED_FRAME_BOUNDS is the *visible* frame;
    # GetWindowRect on Win10+ includes an invisible resize border (~7px per
    # side at 100% DPI) that would shift every cropped screenshot left and
    # up by that much — a systematic error that survives calibration
    # because it is in our own numbers, not the model's.
    DWMWA_CLOAKED = 14
    DWMWA_EXTENDED_FRAME_BOUNDS = 9

    results: list[dict[str, Any]] = []
    foreground = user32.GetForegroundWindow()

    # One probing budget for the whole enumeration, not per window.
    # Hit-testing asks each application to answer on its own UI thread, and
    # SMTO_ABORTIFHUNG only returns early for a thread Windows already
    # considers hung (five seconds without taking a message) — a merely
    # slow window handler still spends the full timeout. Enough of those
    # and the caller times the whole call out and falls back to a
    # full-screen capture, which is the one region this feature exists to
    # avoid. Windows past the budget simply get no raise_point: no
    # measurement is reported as no measurement.
    raise_budget_ends = time.monotonic() + RAISE_PROBE_BUDGET_S

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _class_of(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
        return buf.value

    def _rect_of(hwnd) -> Optional[tuple[int, int, int, int]]:
        rect = wintypes.RECT()
        if dwmapi is not None:
            got = wintypes.RECT()
            hr = dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
                ctypes.byref(got), ctypes.sizeof(got))
            if hr == 0 and got.right > got.left and got.bottom > got.top:
                return (got.left, got.top, got.right, got.bottom)
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)

    def _is_cloaked(hwnd) -> bool:
        # UWP/"modern" apps keep hidden ghost windows that are IsWindowVisible
        # yet not rendered. Without this check the list is full of phantom
        # entries with plausible titles, and picking one crops empty pixels.
        if dwmapi is None:
            return False
        val = ctypes.c_int(0)
        hr = dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(DWMWA_CLOAKED),
            ctypes.byref(val), ctypes.sizeof(val))
        return hr == 0 and val.value != 0

    def _callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(wintypes.HWND(hwnd)):
                return True
            length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 1)
            title = buf.value
            if not title.strip():
                return True
            if _is_cloaked(hwnd):
                return True
            cls = _class_of(hwnd)
            if cls in _WIN32_SHELL_CLASSES:
                # The desktop itself ("Program Manager" / Progman) is a
                # visible, titled, full-virtual-screen window. Left in, it
                # is the widest entry in the list and a plausible-looking
                # pick for "the whole screen" — which is the one region
                # this whole feature exists to avoid.
                return True
            rect = _rect_of(hwnd)
            if rect is None:
                return True
            x1, y1, x2, y2 = rect
            if x2 - x1 < 1 or y2 - y1 < 1:
                return True
            # Minimized windows report (-32000, -32000); they are not a
            # usable screenshot region.
            minimized = x1 <= -30000 or y1 <= -30000
            if minimized and not include_offscreen:
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(
                wintypes.HWND(hwnd), ctypes.byref(pid))
            entry = {
                "title": title,
                "pid": int(pid.value),
                "rect": [x1, y1, x2, y2],
                "foreground": bool(hwnd == foreground),
                "minimized": bool(minimized),
                "class": cls,
                # False while a modal dialog owns this window. The window
                # still looks and screenshots exactly as usual; it just
                # discards every click, a real mouse's included.
                "enabled": bool(user32.IsWindowEnabled(wintypes.HWND(hwnd))),
            }
            if not minimized:
                fraction = _visible_fraction_win32(
                    user32, hwnd, x1, y1, x2, y2)
                if fraction is not None:
                    entry["visible_fraction"] = fraction
                # The foreground window never needs raising, and probing
                # it would spend a shared, finite allowance on the one
                # window that cannot benefit — taking it away from the
                # windows that can.
                if not entry["foreground"]:
                    point = _raise_point_win32(
                        user32, hwnd, x1, y1, x2, y2,
                        deadline=min(
                            raise_budget_ends,
                            time.monotonic() + RAISE_PROBE_PER_WINDOW_S))
                    if point is not None:
                        entry["raise_point"] = list(point)
            results.append(entry)
        except Exception:
            # One bad window must not abort the enumeration.
            return True
        return True

    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    # EnumWindows already walks in Z-order (top first); make the currently
    # focused window unambiguous for callers that just take [0].
    results.sort(key=lambda w: (not w["foreground"],))
    return results


def _visible_fraction_win32(user32, hwnd, x1: int, y1: int,
                            x2: int, y2: int) -> "float | None":
    """Roughly how much of this window is actually the topmost thing.

    Samples a small grid inside the rectangle and asks the OS which window
    owns each point. 1.0 means nothing is covering it; 0.0 means every
    sample belongs to something else, so a screenshot of this rectangle
    would show that something else instead.

    Returns ``None`` when the measurement could not be taken at all — a
    rectangle too small to sample, or every probe failing. That is NOT the
    same as 1.0 and must not be reported as it: "nothing is covering it"
    and "nobody looked" read identically to a caller, and only one of them
    is a reason to go ahead.

    A grid this coarse cannot express "the one button you wanted is under
    a tooltip", and it is not meant to — it exists to catch the case that
    silently ruins everything: the window is behind another window and the
    capture is of the wrong application entirely.
    """
    from ctypes import wintypes

    GA_ROOT = 2
    width = x2 - x1
    height = y2 - y1
    if width < 4 or height < 4:
        return None
    # Inset so the sample points sit inside the frame rather than on the
    # 1px border, where the point can belong to the window behind.
    fractions = (0.2, 0.35, 0.5, 0.65, 0.8)
    hits = 0
    total = 0
    for fx in fractions:
        for fy in fractions:
            px = int(x1 + width * fx)
            py = int(y1 + height * fy)
            try:
                point = wintypes.POINT(px, py)
                under = user32.WindowFromPoint(point)
                if not under:
                    total += 1
                    continue
                root = user32.GetAncestor(wintypes.HWND(under), GA_ROOT)
                total += 1
                if root == hwnd:
                    hits += 1
            except Exception:
                # One unreadable point must not lose the whole measurement.
                continue
    if total == 0:
        return None
    return round(hits / total, 2)


def _raise_point_win32(user32, hwnd, x1: int, y1: int,
                       x2: int, y2: int,
                       deadline: "float | None" = None,
                       ) -> "tuple[int, int] | None":
    """A screen point to click to raise this window.

    "Harmlessly" is not something this can promise, which is the honest
    version of what this function does: it narrows the candidates as far as
    the OS allows and the caller checks the outcome afterwards.

    Two conditions, both necessary and together still not sufficient:

    * the application answers ``WM_NCHITTEST`` there with ``HTCAPTION`` —
      it considers that spot a place to grab the window by. Necessary but
      not sufficient: Chrome's "new tab" button answers ``HTCAPTION`` too,
      which is why the scan starts at the right end (see below); and
    * ``WindowFromPoint`` agrees the spot is actually this window — a
      caption hidden under another window is no use.

    Returns ``None`` when nothing qualifies (a maximised window with no
    caption, a window buried completely, a full-screen app). The caller
    then has to raise it some other way, or say it cannot.

    The query is read-only and sent with a timeout: a hung application
    must not hang the window listing with it.
    """
    import ctypes
    from ctypes import wintypes

    WM_NCHITTEST = 0x0084
    HTCAPTION = 2
    SMTO_ABORTIFHUNG = 0x0002
    GA_ROOT = 2

    width = x2 - x1
    height = y2 - y1
    if width < 80 or height < 24:
        return None

    # Scanned right to left, in fixed steps, and this order is the whole
    # point. HTCAPTION does NOT mean "clicking here only raises the
    # window" — measured on Chrome, its "new tab" button answers
    # HTCAPTION, so taking the leftmost qualifying point opened a tab
    # instead of merely raising: 7460 was the button while 7500 and 7540,
    # further right, were the empty drag strip. The left end of a caption
    # is where tabs, menus and toolbar buttons live; the right end, just
    # inside the window controls, is the likeliest place to be nothing but
    # drag area. The controls themselves are excluded by their own answers
    # (HTMINBUTTON / HTMAXBUTTON / HTCLOSE), so they need no margin — a
    # fixed step walks in past them rather than guessing how wide they are,
    # which varies by application and by DPI.
    left = x1 + min(60, width // 4)
    right = x2 - 8
    if right <= left:
        return None
    # 16 steps of 24px covers the right-hand ~380px. Deliberately NOT
    # falling back to the rest of the strip when nothing there qualifies:
    # the left-hand end is where the buttons that lie about being drag
    # areas live, so "no point found" is the better answer than "a point
    # that might open a tab". The caller degrades safely on None — it
    # works with the window as-is if it is visible enough, and refuses with
    # an explanation if it is not.
    xs = [x for x in (right - 24 * i for i in range(16)) if x > left]
    ys = [y1 + dy for dy in (8, 16, 24, 32) if dy < height]

    result = ctypes.c_size_t(0)

    def qualifies(x, y):
        """The app calls this spot a drag area, and it really is on top."""
        lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        try:
            sent = user32.SendMessageTimeoutW(
                wintypes.HWND(hwnd), wintypes.UINT(WM_NCHITTEST),
                wintypes.WPARAM(0), wintypes.LPARAM(lparam),
                wintypes.UINT(SMTO_ABORTIFHUNG), wintypes.UINT(120),
                ctypes.byref(result))
            if not sent or result.value != HTCAPTION:
                return False
            under = user32.WindowFromPoint(wintypes.POINT(x, y))
            if not under:
                return False
            return user32.GetAncestor(wintypes.HWND(under), GA_ROOT) == hwnd
        except Exception:
            # One unanswerable probe is not a reason to give up on the
            # window, let alone on the whole enumeration.
            return False

    # A lone qualifying answer can still be a self-drawn control that only
    # reports itself as drag area — Chrome's "new tab" button does exactly
    # that, which is what sent this design back to the drawing board. Three
    # consecutive answers span 48px, which a control that size cannot, so
    # the middle of the first run is preferred. A lone answer is kept as a
    # fallback rather than discarded: it is still better than not raising
    # at all, and the caller re-reads the window afterwards either way.
    #
    # x outermost: "rightmost wins" only holds if every row is tried at one
    # x before moving left. With y outermost, a point further left at y=8
    # would beat a better one further right at y=16.
    run = []
    lone = None
    for x in xs:
        hit = None
        for y in ys:
            if deadline is not None and time.monotonic() >= deadline:
                # Out of budget: hand back the best answer found so far
                # rather than throwing it away.
                return run[1] if len(run) >= 3 else lone
            if qualifies(x, y):
                hit = (x, y)
                break
        if hit is None:
            run = []          # a gap breaks the run
            continue
        if lone is None:
            lone = hit
        run.append(hit)
        if len(run) >= 3:
            return run[1]
    return lone


def _list_windows_darwin(include_offscreen: bool) -> list[dict[str, Any]]:
    try:
        import Quartz  # type: ignore
    except Exception as exc:  # pragma: no cover - probed by windows_supported
        raise WindowInfoUnavailable(unsupported_hint()) from exc

    opts = Quartz.kCGWindowListExcludeDesktopElements
    if not include_offscreen:
        opts |= Quartz.kCGWindowListOptionOnScreenOnly
    infos = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []

    results: list[dict[str, Any]] = []
    for info in infos:
        # Layer 0 is the normal application layer; menu bar, dock and
        # overlays live above it and are never click targets we want to
        # crop to.
        if int(info.get("kCGWindowLayer", 0) or 0) != 0:
            continue
        bounds = info.get("kCGWindowBounds") or {}
        x = int(bounds.get("X", 0))
        y = int(bounds.get("Y", 0))
        w = int(bounds.get("Width", 0))
        h = int(bounds.get("Height", 0))
        if w < 1 or h < 1:
            continue
        title = info.get("kCGWindowName") or ""
        app = info.get("kCGWindowOwnerName") or ""
        if not title and not app:
            continue
        results.append({
            "title": title or app,
            "app": app,
            "pid": int(info.get("kCGWindowOwnerPID", 0) or 0),
            "rect": [x, y, x + w, y + h],
            "foreground": False,
            "minimized": False,
        })
    if results:
        # CGWindowList returns front-to-back order.
        results[0]["foreground"] = True
    return results


def find_window(query: str,
                windows: Optional[list[dict[str, Any]]] = None,
                ) -> Optional[dict[str, Any]]:
    """Best window whose title (or macOS app name) contains ``query``.

    Case-insensitive substring match, front-most window wins ties — the
    agent says "the WeChat window", not an HWND, and two chat windows
    with the same title are not an error worth failing the call over.
    """
    if windows is None:
        windows = list_windows()
    needle = query.strip().lower()
    if not needle:
        return None
    exact = [w for w in windows
             if needle == str(w.get("title", "")).strip().lower()]
    if exact:
        return exact[0]
    for w in windows:
        hay = f"{w.get('title', '')} {w.get('app', '')}".lower()
        if needle in hay:
            return w
    return None


# ────────────────────────── calibration markers ──────────────────────────

# Marker geometry, as fractions of the smaller image side. Big enough to
# survive the model's internal downscale (a marker that lands on 3 pixels
# is a coin flip), small enough that two of them do not hide UI.
_MARKER_FRAC = 0.045
_MARKER_MIN = 28
_MARKER_MAX = 72
_INSET = 6          # px from the image edge to the marker's outer edge
_BORDER_FRAC = 0.18  # yellow ring thickness, as a fraction of the side
_DOT_FRAC = 0.34    # white centre dot, as a fraction of the side

_RED = (220, 20, 20)
_YELLOW = (255, 230, 0)
_WHITE = (255, 255, 255)


def marker_size(width: int, height: int) -> int:
    side = int(min(width, height) * _MARKER_FRAC)
    return max(_MARKER_MIN, min(_MARKER_MAX, side))


def _fill(buf: bytearray, w: int, h: int,
          x0: int, y0: int, x1: int, y1: int,
          color: tuple[int, int, int]) -> None:
    """Fill the half-open rect [x0,x1) x [y0,y1) in an RGB byte buffer."""
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(w, x1)
    y1 = min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return
    row = bytes(color) * (x1 - x0)
    stride = w * 3
    for y in range(y0, y1):
        start = y * stride + x0 * 3
        buf[start:start + len(row)] = row


def draw_markers(rgb: bytes, width: int, height: int,
                 ) -> tuple[bytes, list[dict[str, Any]]]:
    """Stamp two calibration markers onto an RGB buffer.

    Returns the modified buffer and the markers' exact centres **in this
    image's pixel space**. Two markers (top-left and bottom-right) rather
    than one: with two known points per axis the model's reported
    coordinates can be fitted as ``reported = scale * actual + offset``,
    which absorbs a constant offset that a single-point ratio silently
    folds into the scale. Measured on this project's own runs, a
    single-point fit put the x and y scales 1.7% apart; the same capture
    with a two-point fit agrees with an accessibility-tree anchor to
    ~0.1%.

    Markers must be drawn *after* every resize. Drawing before means the
    resampler moves and blurs the very thing whose position we claim to
    know exactly.
    """
    buf = bytearray(rgb)
    side = marker_size(width, height)
    border = max(2, int(side * _BORDER_FRAC))
    dot = max(4, int(side * _DOT_FRAC))

    placements = [
        ("tl", _INSET, _INSET),
        ("br", width - _INSET - side, height - _INSET - side),
    ]
    markers: list[dict[str, Any]] = []
    for mid, x0, y0 in placements:
        x0 = max(0, min(x0, width - side))
        y0 = max(0, min(y0, height - side))
        x1, y1 = x0 + side, y0 + side
        _fill(buf, width, height, x0, y0, x1, y1, _YELLOW)
        _fill(buf, width, height,
              x0 + border, y0 + border, x1 - border, y1 - border, _RED)
        cx = x0 + side / 2.0
        cy = y0 + side / 2.0
        _fill(buf, width, height,
              int(cx - dot / 2), int(cy - dot / 2),
              int(cx - dot / 2) + dot, int(cy - dot / 2) + dot, _WHITE)
        markers.append({
            "id": mid,
            "center": [round(cx, 1), round(cy, 1)],
            "size": side,
            "dot_size": dot,
        })
    return bytes(buf), markers


MARKER_PROMPT_HINT = (
    "Two calibration markers were drawn on this image: red squares with a "
    "yellow ring and a white square at the exact centre, one near the "
    "top-left corner and one near the bottom-right. Report the centre of "
    "each white square along with the target — the caller uses them to "
    "convert your coordinates back to screen pixels."
)
