"""`screen.windows` and the calibration markers.

The occlusion measurement here has a real incident behind it: a calculator
sitting behind an editor was captured by its own rectangle, so the capture
was of the editor. A vision model asked about it answered "there is no 8
key here, this is a file explorer" — completely correct, completely
useless, and indistinguishable downstream from a real miss. Reporting how
much of a window is actually on top is what turns that into an error a
caller can act on.
"""

import json
import sys
import time
from unittest import mock

import pytest

from clawtouch_mcp import screen as screen_mod
from clawtouch_mcp.server import ClawTouchMcpServer, ServerConfig

# `ctypes.wintypes` defines a Windows-only type code, so importing it —
# which these helpers do — fails outright on Linux. The helpers are Win32
# by definition; the CI matrix runs ubuntu and macOS too, and a test that
# only ever passed on the author's machine is how a green local run turns
# into a red release. (Same shape as the 0.4.5 macOS-only CI break.)
windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="exercises Win32-only helpers")


def _text(result: dict) -> dict:
    """The JSON metadata clawtouch-mcp returns alongside any content."""
    body = [c for c in result["content"] if c["type"] == "text"][0]["text"]
    return json.loads(body)


@pytest.fixture()
def server():
    return ClawTouchMcpServer(
        ServerConfig(mock=True, allow_screenshot=True,
                     screen_w=1920, screen_h=1080))


async def _call(server, name, args):
    resp = await server.dispatch({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })
    return resp["result"]


class TestScreenWindowsTool:
    async def test_registered_only_with_the_screenshot_flag(self):
        """One flag gates both screen tools. Window titles are the same
        order of disclosure as the pixels showing them, and a second flag
        would only be a second thing to forget."""
        off = ClawTouchMcpServer(ServerConfig(mock=True))
        assert "screen.windows" not in off.tools
        on = ClawTouchMcpServer(
            ServerConfig(mock=True, allow_screenshot=True))
        assert "screen.windows" in on.tools

    async def test_unsupported_platform_answers_instead_of_raising(
            self, server, monkeypatch):
        monkeypatch.setattr(screen_mod, "windows_supported", lambda: False)
        payload = _text(await _call(server, "screen.windows", {}))
        assert "error" in payload
        assert payload["platform"]

    async def test_an_unreadable_window_list_is_an_error_not_an_empty_one(
            self, server, monkeypatch):
        """The whole chain, not just the darwin backend: a listing that
        could not be read must reach the caller AS a failure.

        An empty `windows` array is an answer — "I looked, nothing is
        open" — and the one thing this must never become. Pinned at this
        layer because the backend raising is worth nothing if the tool
        above it turns the exception back into `{"windows": [], ...}`.
        """
        monkeypatch.setattr(screen_mod, "windows_supported", lambda: True)

        def _boom(**_):
            raise screen_mod.WindowInfoUnavailable(
                "CGWindowListCopyWindowInfo gave NULL — the window server "
                "could not be reached")

        monkeypatch.setattr(screen_mod, "list_windows", _boom)
        payload = _text(await _call(server, "screen.windows", {}))
        assert "windows" not in payload and "count" not in payload
        assert "window server" in payload["error"]
        # And no `available`: that key is how the dsh plugin tells a
        # genuine miss from a listing it could not read. Sending an empty
        # one here would make an outage look like an empty desktop, one
        # layer up, where nothing can tell the difference any more.
        assert "available" not in payload

    async def test_an_unreadable_list_does_not_deny_a_named_window_either(
            self, server, monkeypatch):
        """Same failure, asked with a title: "no visible window matching
        X" would be a claim about an application that may well be right
        there."""
        monkeypatch.setattr(screen_mod, "windows_supported", lambda: True)

        def _boom(**_):
            raise screen_mod.WindowInfoUnavailable("window server gone")

        monkeypatch.setattr(screen_mod, "list_windows", _boom)
        payload = _text(await _call(server, "screen.windows",
                                    {"title": "Calculator"}))
        assert "no visible window matching" not in payload["error"]
        assert "window server" in payload["error"]
        assert "available" not in payload

    async def test_a_genuine_miss_is_the_one_that_carries_available(
            self, server, monkeypatch):
        """The other side of the same contract, pinned next to it: only a
        real miss sends `available`, which is what makes it usable as the
        discriminator downstream."""
        monkeypatch.setattr(screen_mod, "windows_supported", lambda: True)
        monkeypatch.setattr(screen_mod, "list_windows", lambda **_: [
            {"title": "Calc", "rect": [0, 0, 100, 100], "foreground": True},
        ])
        payload = _text(await _call(server, "screen.windows",
                                    {"title": "Nope"}))
        assert "no visible window matching" in payload["error"]
        assert payload["available"] == ["Calc"]

    async def test_no_match_lists_what_is_there(self, server, monkeypatch):
        """The available titles ARE the actionable part of the failure:
        "WeChat" vs "微信" is the usual cause, and an agent that can see
        the list retries correctly instead of guessing again."""
        monkeypatch.setattr(screen_mod, "windows_supported", lambda: True)
        monkeypatch.setattr(screen_mod, "list_windows", lambda **_: [
            {"title": "微信", "rect": [0, 0, 100, 100], "foreground": True},
        ])
        payload = _text(await _call(server, "screen.windows",
                                    {"title": "WeChat"}))
        assert "error" in payload
        assert payload["available"] == ["微信"]

    async def test_title_match_returns_the_one_window(
            self, server, monkeypatch):
        monkeypatch.setattr(screen_mod, "windows_supported", lambda: True)
        monkeypatch.setattr(screen_mod, "list_windows", lambda **_: [
            {"title": "Calculator", "rect": [10, 20, 110, 220],
             "foreground": False, "visible_fraction": 0.0},
            {"title": "Editor", "rect": [0, 0, 800, 600],
             "foreground": True, "visible_fraction": 1.0},
        ])
        payload = _text(await _call(server, "screen.windows",
                                    {"title": "calc"}))
        assert payload["window"]["title"] == "Calculator"
        # The covered window is still returned — refusing to look at it is
        # the caller's policy, not this layer's. What this layer owes the
        # caller is the fact that it is covered.
        assert payload["window"]["visible_fraction"] == 0.0


class TestFindWindowPrefersTheMeasuredFrontOne:
    """The same 32-pixel crop this module stopped producing, arrived at by
    the OTHER road.

    An untitled window is listed under its application's NAME, so VS
    Code's 1512x32 service strip matches `find_window("Code")` EXACTLY
    while the editor window the user means matches only loosely
    ("proj - Code"). Ranking exactness above front-most handed back the
    strip — ignoring a `foreground` that had already been measured
    correctly one function earlier.

    The docstring promised "front-most window wins ties" all along; it was
    the list order on Windows that made it true there, and CGWindowList
    order on macOS that made it false.
    """

    STRIP = {"title": "Code", "app": "Code",
             "rect": [0, 0, 1512, 32], "foreground": False}
    MAIN = {"title": "proj - Code", "app": "Code",
            "rect": [0, 0, 1512, 950], "foreground": True}

    def test_the_front_window_beats_an_exact_match_on_a_service_strip(self):
        got = screen_mod.find_window("Code", [self.STRIP, self.MAIN])
        assert got is self.MAIN

    def test_order_in_the_list_does_not_decide_it(self):
        got = screen_mod.find_window("Code", [self.MAIN, self.STRIP])
        assert got is self.MAIN

    def test_exactness_still_wins_between_two_front_windows(self):
        loose = {"title": "Code helper", "app": "", "foreground": True}
        exact = {"title": "Code", "app": "", "foreground": True}
        assert screen_mod.find_window("Code", [loose, exact]) is exact

    def test_an_unmeasured_foreground_falls_back_to_platform_order(self):
        """Absent is not a preference either way — the platform's own
        order remains the answer, which is what it always was."""
        strip = {k: v for k, v in self.STRIP.items() if k != "foreground"}
        main = {k: v for k, v in self.MAIN.items() if k != "foreground"}
        assert screen_mod.find_window("Code", [strip, main]) is strip

    def test_a_background_window_of_another_app_does_not_win(self):
        other = {"title": "Code", "app": "Other", "foreground": False}
        assert screen_mod.find_window("Code", [other, self.MAIN]) is self.MAIN

    def test_nothing_matching_is_still_none(self):
        assert screen_mod.find_window("Nope", [self.STRIP, self.MAIN]) is None

    def test_an_empty_query_is_none(self):
        assert screen_mod.find_window("   ", [self.STRIP]) is None


class TestWindowsStillMeasuresMinimized:
    """The reference implementation is the thing macOS is measured
    against, so it has to be pinned too — otherwise "macOS omits it
    because it does not ask" quietly becomes "nobody reports it".

    Windows-only by nature: the field comes from the real enumeration.
    That is enough, because the Windows runner is in the public matrix.
    """

    def test_the_predicate_itself_says_what_minimized_MEANS(self):
        """Runs everywhere, because the live enumeration can only pin this
        on a desktop that happens to have a minimized window — and forcing
        every entry to False passed that kind of test.

        Windows parks a minimized window at (-32000, -32000); the guard
        is -30000 so a window merely dragged off the left edge is not
        mistaken for one.
        """
        assert screen_mod._minimized_win32(-32000, -32000) is True
        assert screen_mod._minimized_win32(-32000, 100) is True
        assert screen_mod._minimized_win32(100, -32000) is True
        assert screen_mod._minimized_win32(0, 0) is False
        assert screen_mod._minimized_win32(-100, -100) is False
        assert screen_mod._minimized_win32(-29999, -29999) is False

    @windows_only
    def test_only_minimized_windows_are_the_difference_between_the_lists(
            self):
        """The filter is `minimized and not include_offscreen`, so the
        entries `include_offscreen` adds can only be the minimized ones.
        Vacuous on a desktop with none — which is why the predicate above
        carries the real weight."""
        default = screen_mod.list_windows()
        everything = screen_mod.list_windows(include_offscreen=True)
        seen = {(w["pid"], w["title"], tuple(w["rect"])) for w in default}
        for win in everything:
            if (win["pid"], win["title"], tuple(win["rect"])) not in seen:
                assert win["minimized"] is True, win

    @windows_only
    def test_every_entry_carries_a_measured_minimized(self):
        """No assertion that the desktop HAS a window: a CI runner may
        legitimately have none that survives the enumeration's own filters
        (untitled, cloaked, shell classes), and a test that goes red on an
        empty desktop is a flake, not a pin. What is pinned is the shape of
        whatever comes back."""
        for win in screen_mod.list_windows():
            assert isinstance(win.get("minimized"), bool), win

    @windows_only
    def test_include_offscreen_is_what_admits_them(self):
        """`minimized and not include_offscreen` is the filter under test,
        so this actually calls it both ways.

        Host-dependent by nature: a runner whose desktop has no minimized
        window can only demonstrate the weaker half. So what is asserted
        is the RELATION, which holds either way — the default listing
        never contains a minimized window, and admitting them can only
        add. The strong, host-independent pinning of this rule is on the
        darwin side, where a fake CGWindowList makes the population a
        fixture rather than a fact about the machine.
        """
        default = screen_mod.list_windows()
        everything = screen_mod.list_windows(include_offscreen=True)
        assert not any(w["minimized"] for w in default), (
            "the default listing is what promises to exclude them")
        assert len(everything) >= len(default)
        for win in everything:
            assert isinstance(win.get("minimized"), bool), win


@windows_only
class TestVisibleFraction:
    """The measurement itself, against a stubbed `WindowFromPoint`."""

    def _user32(self, owner_of_point):
        class Fake:
            def WindowFromPoint(self, point):  # noqa: N802 - Win32 name
                return owner_of_point(point.x, point.y)

            def GetAncestor(self, hwnd, _flag):  # noqa: N802 - Win32 name
                # The stub hands back top-level handles already.
                return hwnd.value if hasattr(hwnd, "value") else hwnd
        return Fake()

    def test_fully_on_top_is_one(self):
        u = self._user32(lambda x, y: 42)
        assert screen_mod._visible_fraction_win32(u, 42, 0, 0, 200, 200) == 1.0

    def test_fully_covered_is_zero(self):
        u = self._user32(lambda x, y: 7)
        assert screen_mod._visible_fraction_win32(u, 42, 0, 0, 200, 200) == 0.0

    def test_half_covered_is_between(self):
        # Anything past the middle belongs to the window in front.
        u = self._user32(lambda x, y: 42 if x < 100 else 7)
        got = screen_mod._visible_fraction_win32(u, 42, 0, 0, 200, 200)
        assert 0.2 < got < 0.8, got

    def test_a_rect_too_small_to_sample_reports_no_measurement(self):
        """Not 1.0: "nothing is covering it" and "nobody looked" are
        different answers, and only one of them says go ahead."""
        u = self._user32(lambda x, y: 7)
        assert screen_mod._visible_fraction_win32(u, 42, 5, 5, 6, 6) is None

    def test_every_probe_failing_reports_no_measurement(self):
        class Unreadable:
            def WindowFromPoint(self, _p):  # noqa: N802
                raise OSError("cannot read the desktop")

            def GetAncestor(self, h, _f):  # noqa: N802
                return h
        assert screen_mod._visible_fraction_win32(
            Unreadable(), 42, 0, 0, 200, 200) is None


class TestDisabledWindow:
    """A window can be fully visible and still refuse every click.

    Cost of not reporting this, measured once: half an hour of blaming the
    coordinates, the click timing, the vision model and the device, on a
    WeChat window that a hidden modal dialog had disabled. A physical mouse
    could not click it either.
    """

    async def test_disabled_window_is_reported(self, server, monkeypatch):
        monkeypatch.setattr(screen_mod, "windows_supported", lambda: True)
        monkeypatch.setattr(screen_mod, "list_windows", lambda **_: [
            {"title": "WeChat", "rect": [0, 0, 800, 600], "foreground": True,
             "visible_fraction": 1.0, "enabled": False},
        ])
        payload = _text(await _call(server, "screen.windows", {}))
        win = payload["windows"][0]
        # Fully visible AND fully unusable — the two facts are independent,
        # so neither may stand in for the other.
        assert win["visible_fraction"] == 1.0
        assert win["enabled"] is False


@windows_only
class TestRaisePoint:
    """A point to click to raise the window, chosen to limit what else
    a click there could do.

    "The top strip is the title bar" is not good enough: in a browser that
    strip is the tabs, and a raise would open one. The application's own
    answer to WM_NCHITTEST is what makes the point safe.
    """

    def _user32(self, hit, owner=42):
        class Fake:
            def SendMessageTimeoutW(self, hwnd, msg, wparam, lparam,
                                    flags, timeout, out):  # noqa: N802
                x = lparam.value & 0xFFFF
                y = (lparam.value >> 16) & 0xFFFF
                out._obj.value = hit(x, y)
                return 1

            def WindowFromPoint(self, point):  # noqa: N802
                return owner

            def GetAncestor(self, hwnd, _flag):  # noqa: N802
                return hwnd.value if hasattr(hwnd, "value") else hwnd
        return Fake()

    HTCAPTION = 2
    HTCLIENT = 1

    def test_a_caption_point_is_returned(self):
        u = self._user32(lambda x, y: self.HTCAPTION)
        got = screen_mod._raise_point_win32(u, 42, 0, 0, 900, 600)
        assert got is not None
        # Inside the caption strip, and clear of both ends where the app
        # icon and the minimise/maximise/close buttons live.
        assert 0 < got[1] <= 32, got
        # The exact contract, not just "somewhere on the right". Every
        # answer qualifies here, so the first run of three consecutive
        # answers is the three rightmost probes, and its MIDDLE is what
        # comes back: a lone answer could be a 24px control that lies about
        # being a drag area, three consecutive ones span 48px and cannot be.
        assert got == (900 - 8 - 24, 8), got

    def test_a_button_claiming_caption_does_not_win_over_the_drag_strip(self):
        """HTCAPTION does not mean "clicking here only raises the window".

        Measured on Chrome: its "new tab" button answers HTCAPTION, so
        taking the leftmost qualifying point opened a tab instead of
        raising the window. The scan therefore runs right to left, where
        the strip just inside the window controls is drag area and nothing
        else.
        """
        # Chrome's measured shape, in a 1000px-wide window:
        #   >= 940    the window controls  (excluded by their own answers)
        #   860..940  empty drag strip     <- the safe point
        #   830..860  "new tab" button     <- answers HTCAPTION, opens a tab
        #   < 830     the tabs             (client area)
        newtab = range(830, 860)

        def hit(x, _y):
            if x >= 940:
                return 20               # HTCLOSE / HTMAXBUTTON / HTMINBUTTON
            if x >= 860 or x in newtab:
                return self.HTCAPTION
            return self.HTCLIENT

        got = screen_mod._raise_point_win32(
            self._user32(hit), 42, 0, 0, 1000, 600)
        assert got is not None
        assert got[0] not in newtab, f"landed on the new-tab button: {got}"
        assert 860 <= got[0] < 940, got

    def test_a_point_further_right_wins_even_on_a_lower_row(self):
        """"Rightmost wins" has to hold across rows, not just within one.

        With the row loop outermost, a point further LEFT on the first row
        would beat a better one further right on the second — which is how
        a button that claims to be a drag area gets picked despite the
        right-to-left scan. So x is the outer loop.
        """
        def hit(x, y):
            if (x, y) == (968, 16):      # further right, second row
                return self.HTCAPTION
            if (x, y) == (800, 8):       # further left, first row
                return self.HTCAPTION
            return self.HTCLIENT

        got = screen_mod._raise_point_win32(
            self._user32(hit), 42, 0, 0, 1000, 600)
        assert got == (968, 16), got

    def test_a_run_of_three_beats_a_lone_answer_further_right(self):
        """The protection against a control that lies about being a drag
        area: Chrome's "new tab" button answers HTCAPTION, and it is about
        24px wide. Three consecutive answers span 48px, so a run cannot be
        a control that size — and a run is preferred over a lone answer
        even when the lone one sits further right.
        """
        lone_x = 968                      # an isolated 24px "control"
        run_xs = {896, 872, 848}          # a real, wider drag strip

        def hit(x, _y):
            return (self.HTCAPTION if x == lone_x or x in run_xs
                    else self.HTCLIENT)

        got = screen_mod._raise_point_win32(
            self._user32(hit), 42, 0, 0, 1000, 600)
        assert got == (872, 8), got       # the middle of the run
        assert got[0] != lone_x, "a lone answer must not win over a run"

    def test_a_lone_answer_is_still_used_when_there_is_no_run(self):
        """Falling back rather than refusing: a single qualifying point is
        still better than not raising at all, and the caller re-reads the
        window afterwards either way."""
        def hit(x, _y):
            return self.HTCAPTION if x == 968 else self.HTCLIENT

        got = screen_mod._raise_point_win32(
            self._user32(hit), 42, 0, 0, 1000, 600)
        assert got == (968, 8), got

    def test_a_client_area_answer_is_refused(self):
        """An app that calls the whole strip client area (a browser's tab
        row) gets no raise point rather than a click on a tab."""
        u = self._user32(lambda x, y: self.HTCLIENT)
        assert screen_mod._raise_point_win32(u, 42, 0, 0, 900, 600) is None

    def test_a_covered_caption_is_refused(self):
        """WM_NCHITTEST answers about geometry, not about what is on top;
        a caption under another window is no use for raising."""
        u = self._user32(lambda x, y: self.HTCAPTION, owner=999)
        assert screen_mod._raise_point_win32(u, 42, 0, 0, 900, 600) is None

    def test_a_window_too_small_to_probe_safely_is_refused(self):
        u = self._user32(lambda x, y: self.HTCAPTION)
        assert screen_mod._raise_point_win32(u, 42, 0, 0, 70, 400) is None

    def test_the_probe_budget_stops_the_search(self):
        """One unresponsive application must not hold the listing up.

        Hit-testing runs on the target application's own UI thread, and a
        merely slow handler spends the whole per-probe timeout rather than
        aborting. Past the shared budget a window simply gets no raise
        point, which reads as no measurement rather than as a safe one.
        """
        probes = []

        class Counting:
            def SendMessageTimeoutW(self, *a):  # noqa: N802
                probes.append(1)
                return 0

        got = screen_mod._raise_point_win32(
            Counting(), 42, 0, 0, 900, 600,
            deadline=time.monotonic() - 1)
        assert got is None
        assert probes == [], "nothing should be probed past the deadline"

    def test_a_live_budget_does_not_interfere(self):
        u = self._user32(lambda x, y: self.HTCAPTION)
        got = screen_mod._raise_point_win32(
            u, 42, 0, 0, 900, 600, deadline=time.monotonic() + 30)
        assert got is not None

    def test_an_unanswerable_probe_does_not_raise(self):
        class Hung:
            def SendMessageTimeoutW(self, *a):  # noqa: N802
                raise OSError("window is hung")

            def WindowFromPoint(self, point):  # noqa: N802
                return 42

            def GetAncestor(self, hwnd, _flag):  # noqa: N802
                return hwnd
        assert screen_mod._raise_point_win32(Hung(), 42, 0, 0, 900, 600) is None


_SECOND_UNSET = object()


class _FakeQuartz:
    """Just enough CGWindowList to drive `_list_windows_darwin` anywhere.

    The darwin path imports Quartz inside the function, so injecting a
    module is all it takes — which means these run on the ubuntu and
    windows CI runners too, instead of skipping exactly where the public
    matrix has no pyobjc and therefore never exercises them at all.
    """

    kCGWindowListExcludeDesktopElements = 1 << 4
    kCGWindowListOptionOnScreenOnly = 1 << 0
    kCGNullWindowID = 0

    def __init__(self, first, second=_SECOND_UNSET):
        self._first = first
        self._second = first if second is _SECOND_UNSET else second
        self.options: list[int] = []

    def CGWindowListCopyWindowInfo(self, options, _relative):  # noqa: N802
        self.options.append(options)
        return self._first if len(self.options) == 1 else self._second


def _win(pid, *, wid, title="A window", app="An app", w=800, h=600,
         onscreen=True, layer=0):
    info = {
        "kCGWindowLayer": layer,
        "kCGWindowBounds": {"X": 0, "Y": 0, "Width": w, "Height": h},
        "kCGWindowOwnerPID": pid,
        "kCGWindowNumber": wid,
        "kCGWindowOwnerName": app,
    }
    if title:
        info["kCGWindowName"] = title
    if onscreen:
        info["kCGWindowIsOnscreen"] = True
    return info


def _list(monkeypatch, quartz, front_pid, *, include_offscreen=False):
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr(screen_mod, "_frontmost_pid_darwin",
                        lambda: front_pid)
    return screen_mod._list_windows_darwin(include_offscreen)


class TestForegroundIsAbsentWhenUnasked:
    """The two ways to have no foreground window are NOT the same, and the
    field has to tell them apart.

    Measured on macOS 26 for every case that actually occurs: with the
    screen LOCKED `NSWorkspace.frontmostApplication()` answers
    loginwindow's pid (not nil); an application whose windows are all
    minimised, or all on another Space, still answers with its own pid.
    All of those are real answers, so `False` on every window is a true
    statement. That leaves absence for the case it belongs to — the
    question not being answerable — where `False` would assert something
    nobody checked.
    """

    def test_a_failed_query_drops_the_field_entirely(self, monkeypatch):
        wins = _list(monkeypatch, _FakeQuartz([_win(1, wid=10)]), None)
        assert wins and all("foreground" not in w for w in wins)

    def test_an_answered_query_with_no_candidate_keeps_false(self,
                                                             monkeypatch):
        """The OS answered, and "none of these is in front" is what it
        answered — a true statement, so the field stays."""
        wins = _list(monkeypatch, _FakeQuartz([_win(1, wid=10)]), 999)
        assert wins and all(w["foreground"] is False for w in wins)

    def test_a_pid_of_minus_one_is_not_an_answer(self, monkeypatch):
        """`processIdentifier()` documents -1 for an application with no
        pid. It is truthy, so letting it through would correlate against
        nothing and leave `False` standing on every window off the back
        of a lookup that never happened."""
        monkeypatch.setattr(screen_mod, "_frontmost_pid_darwin",
                            lambda: None)  # what the -1 guard now returns
        wins = _list(monkeypatch, _FakeQuartz([_win(1, wid=10)]), None)
        assert all("foreground" not in w for w in wins)

    def test_the_minus_one_guard_is_in_the_query_itself(self):
        """Pinned separately from the plumbing above: the mapping from a
        negative pid to "unanswerable" has to live in the query."""
        class _App:
            def processIdentifier(self):  # noqa: N802
                return -1

        class _WS:
            @staticmethod
            def sharedWorkspace():  # noqa: N802
                return _WS()

            def frontmostApplication(self):  # noqa: N802
                return _App()

        import types
        fake = types.ModuleType("AppKit")
        fake.NSWorkspace = _WS
        with mock.patch.dict(sys.modules, {"AppKit": fake}):
            assert screen_mod._frontmost_pid_darwin() is None

    def test_at_most_one_window_is_flagged(self, monkeypatch):
        wins = _list(monkeypatch, _FakeQuartz(
            [_win(7, wid=10), _win(7, wid=11), _win(8, wid=12)]), 7)
        assert len([w for w in wins if w.get("foreground")]) == 1

    def test_the_real_query_answers_or_says_it_cannot(self):
        """Contract check against the real OS: a pid, a real 0, or None —
        never an exception, because the caller has no other way to
        degrade. Safe without pyobjc too: that is exactly the None case.
        """
        pid = screen_mod._frontmost_pid_darwin()
        assert pid is None or (isinstance(pid, int) and pid >= 0)


class TestOrderingListing:
    """Order only means anything in an on-screen listing, so that is where
    the pick comes from — including under `include_offscreen=True`, where
    the listing spans other Spaces and dropping the off-screen entries
    would NOT restore the z-order of the ones that remain."""

    def test_offscreen_mode_uses_the_second_listings_ORDER(self,
                                                            monkeypatch):
        """The point is not that a second call happens — it is that its
        ORDER is the one used. The two listings are therefore given
        opposite orders here, so a revert to "reuse the listing we already
        have" flags the other window and this fails. Asserting only that
        the call was made let exactly that revert pass.
        """
        # Three windows, and the winner is the MIDDLE of `first` — neither
        # end, and neither the lowest nor the highest id. That kills the
        # whole family of "made the second call, then reordered the list it
        # already had" mutants (identity, reverse, sort-by-id either way),
        # not just the reverse.
        first = [_win(7, wid=10, title="Wrong one"),
                 _win(7, wid=11, title="Right one"),
                 _win(7, wid=12, title="Another wrong one")]
        second = [_win(7, wid=11, title="Right one"),
                  _win(7, wid=12, title="Another wrong one"),
                  _win(7, wid=10, title="Wrong one")]
        # (winner = first[1] here; the mirror case below deliberately puts
        # the winner at a DIFFERENT index of `first`, so "made the call and
        # then took infos[1]" cannot satisfy both.)
        quartz = _FakeQuartz(first, second=second)
        wins = _list(monkeypatch, quartz, 7, include_offscreen=True)
        assert len(quartz.options) == 2, "the ordering query must happen"
        assert quartz.options[1] & _FakeQuartz.kCGWindowListOptionOnScreenOnly
        flagged = [w for w in wins if w.get("foreground")]
        assert [w["title"] for w in flagged] == ["Right one"]

    def test_the_offscreen_listings_own_order_is_not_used(self, monkeypatch):
        """The mirror image, so neither direction can drift: swap which
        listing holds which order and the answer follows the second one
        again."""
        # Winner is first[2] this time, not first[1]: together with the
        # case above, no fixed index into the listing already in hand can
        # satisfy both, and neither can reversing or sorting it.
        first = [_win(7, wid=11, title="Wrong one"),
                 _win(7, wid=12, title="Another wrong one"),
                 _win(7, wid=10, title="Right one")]
        second = [_win(7, wid=10, title="Right one"),
                  _win(7, wid=11, title="Wrong one"),
                  _win(7, wid=12, title="Another wrong one")]
        wins = _list(monkeypatch, _FakeQuartz(first, second=second), 7,
                     include_offscreen=True)
        flagged = [w for w in wins if w.get("foreground")]
        assert [w["title"] for w in flagged] == ["Right one"]

    def test_the_default_path_asks_only_once(self, monkeypatch):
        quartz = _FakeQuartz([_win(7, wid=10)])
        _list(monkeypatch, quartz, 7)
        assert len(quartz.options) == 1

    def test_a_null_second_listing_drops_the_field(self, monkeypatch):
        """NULL is the documented failure return and is NOT a successful
        empty listing. Collapsing them left every entry asserting
        `foreground: False` off a query that never answered."""
        quartz = _FakeQuartz([_win(7, wid=10)], second=None)
        wins = _list(monkeypatch, quartz, 7, include_offscreen=True)
        assert wins and all("foreground" not in w for w in wins)

    def test_an_empty_second_listing_is_a_real_answer(self, monkeypatch):
        """Nothing on screen is something the OS can truthfully say."""
        quartz = _FakeQuartz([_win(7, wid=10)], second=[])
        wins = _list(monkeypatch, quartz, 7, include_offscreen=True)
        assert wins and all(w["foreground"] is False for w in wins)

    def test_a_window_worth_ordering_by_but_not_reporting_is_skipped(
            self, monkeypatch):
        """The two listings are filtered differently: a zero-sized window
        can order ahead and then never appear in the results. Picking one
        id and hoping meant nothing got flagged while a good window sat
        right behind it."""
        quartz = _FakeQuartz([
            _win(7, wid=10, title="Ghost", w=0, h=0),   # dropped from results
            _win(7, wid=11, title="Real"),
        ])
        wins = _list(monkeypatch, quartz, 7)
        assert [w["title"] for w in wins] == ["Real"]
        assert wins[0]["foreground"] is True

    def test_an_untitled_front_window_still_wins_over_another_app(
            self, monkeypatch):
        """Without Screen Recording permission macOS blanks every title;
        the untitled entries stay in the running rather than handing the
        flag to a different application."""
        quartz = _FakeQuartz([
            _win(9, wid=20, title="Other app"),
            _win(7, wid=21, title=""),
        ])
        wins = _list(monkeypatch, quartz, 7)
        flagged = [w for w in wins if w.get("foreground")]
        assert len(flagged) == 1 and flagged[0]["pid"] == 7


class TestTheWindowListItselfCanBeUnavailable:
    """NULL from the FIRST listing is the same class of lie as everything
    else here, one level up: an empty list reads as an answer.

    Apple separates the two returns — no matching windows gives an empty
    array, NULL means the window server could not be reached, which is
    what a process with no GUI security session gets (started over SSH, or
    from a launchd daemon). `or []` turned "could not look" into "looked,
    and the desktop is empty", and `screen.windows` then answered
    `{"windows": [], "count": 0}` — or, with a title, "no visible window
    matching X" about an application that was right there.

    Not reproduced on hardware: forcing a session without window-server
    access needs the machine's SSH configuration changed. The contract is
    Apple's, and the second listing in this same function already honoured
    it, which is what made the front door inconsistent.
    """

    def test_a_null_first_listing_raises_instead_of_answering_empty(
            self, monkeypatch):
        quartz = _FakeQuartz(None)
        monkeypatch.setitem(sys.modules, "Quartz", quartz)
        monkeypatch.setattr(screen_mod, "_frontmost_pid_darwin", lambda: 7)
        with pytest.raises(screen_mod.WindowInfoUnavailable) as exc:
            screen_mod._list_windows_darwin(False)
        # The hint has to separate this from "your platform is unsupported"
        # and from "nothing is open" — different problems, different fixes.
        assert "window server" in str(exc.value)
        assert "NULL" in str(exc.value)

    def test_an_empty_first_listing_is_a_real_answer(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "Quartz", _FakeQuartz([]))
        monkeypatch.setattr(screen_mod, "_frontmost_pid_darwin", lambda: 7)
        assert screen_mod._list_windows_darwin(False) == []


class TestMinimizedIsNotAnsweredOnDarwin:
    """`include_offscreen` exists to admit minimized windows, and every
    macOS entry used to answer `minimized: false` — the flag's whole
    purpose denied by its own results. Windows measures this; macOS is
    never asked, so the key is absent rather than guessed.

    Deliberately NOT inferred from `kCGWindowIsOnscreen`: off-screen on
    macOS also covers "on another Space", which is a different state.
    """

    def test_the_key_is_absent_rather_than_false(self, monkeypatch):
        wins = _list(monkeypatch, _FakeQuartz([_win(7, wid=10)]), 7)
        assert wins and all("minimized" not in w for w in wins)

    def test_it_stays_absent_for_a_window_that_is_not_on_screen(
            self, monkeypatch):
        """The case the whole rule is about, and the one that was never
        run: CGWindowList marks a minimized window by OMITTING
        `kCGWindowIsOnscreen`, so a fixture that sets it exercises nothing.
        Proven by mutation — adding `if not info.get("kCGWindowIsOnscreen"):
        entry["minimized"] = True` used to survive the entire suite,
        because that branch was reached zero times.

        The ordering listing is passed separately: it is on-screen-only by
        construction, so mirroring the first listing into it would hand it
        an entry no real machine could put there.
        """
        quartz = _FakeQuartz([_win(7, wid=10, onscreen=False)], second=[])
        wins = _list(monkeypatch, quartz, 7, include_offscreen=True)
        assert wins, "the window is off-screen, not filtered out"
        assert all("minimized" not in w for w in wins)
        # And absent means absent — not False, and not inferred from the
        # missing on-screen key, which also covers "on another Space".
        assert all(w["foreground"] is False for w in wins)

    def test_it_stays_absent_for_an_ON_screen_window_in_that_mode_too(
            self, monkeypatch):
        """Both halves of the mode, because they are separately reachable:
        `include_offscreen` returns on-screen AND off-screen windows, and
        a rule applied to only one of them is not the rule. Pinned after a
        mutant that set `minimized: false` on just the on-screen entries
        passed the whole suite."""
        quartz = _FakeQuartz([_win(7, wid=10, onscreen=True),
                              _win(7, wid=11, onscreen=False)])
        wins = _list(monkeypatch, quartz, 7, include_offscreen=True)
        assert len(wins) == 2
        assert all("minimized" not in w for w in wins)


class TestForegroundOnDarwin:
    """`foreground` is the only window fact macOS still answers, so it has
    to be one that was measured.

    It used to be `results[0] = True` on CGWindowList order. That order is
    real — topmost first — but it includes every layer-0 window an app
    owns, and the service windows sort ahead of the one the user is
    looking at: measured on macOS 26, VS Code's first entry is a 1512x32
    strip and the editor window sorts second. The dsh plugin picks its
    capture region from this flag, so the strip became the crop.
    """

    # (owner_pid, had_a_real_title, window_number)
    def test_the_front_apps_titled_window_wins_over_its_service_strip(self):
        cands = [(51133, False, 11), (51133, True, 12)]
        assert screen_mod._foreground_wids_darwin(cands, 51133)[0] == 12

    def test_the_untitled_ones_stay_in_the_running_behind_it(self):
        cands = [(51133, False, 11), (51133, True, 12)]
        assert screen_mod._foreground_wids_darwin(cands, 51133) == [12, 11]

    def test_another_apps_window_is_never_a_candidate(self):
        cands = [(4242, True, 7), (51133, True, 12)]
        assert screen_mod._foreground_wids_darwin(cands, 51133) == [12]

    def test_no_candidates_when_the_front_app_owns_nothing_listed(self):
        cands = [(1, True, 3), (2, True, 4)]
        assert screen_mod._foreground_wids_darwin(cands, 999) == []

    def test_no_candidates_when_there_is_no_frontmost_app(self):
        cands = [(1, True, 3), (2, True, 4)]
        assert screen_mod._foreground_wids_darwin(cands, 0) == []

    def test_an_empty_list_is_not_an_index_error(self):
        assert screen_mod._foreground_wids_darwin([], 51133) == []


class TestOnScreenCandidates:
    def test_off_screen_and_non_zero_layer_entries_are_dropped(self):
        infos = [
            _win(1, wid=1, layer=25),          # menu bar
            _win(2, wid=2, onscreen=False),    # key absent
            _win(4, wid=4, title="Real"),
        ]
        assert screen_mod._onscreen_candidates_darwin(infos) == [(4, True, 4)]

    def test_the_title_flag_survives_the_filter(self):
        assert screen_mod._onscreen_candidates_darwin(
            [_win(9, wid=90, title="")]) == [(9, False, 90)]


class TestMarkers:
    def test_centres_are_reported_where_they_were_drawn(self):
        w, h = 320, 240
        rgb, marks = screen_mod.draw_markers(bytes(w * h * 3), w, h)
        assert [m["id"] for m in marks] == ["tl", "br"]
        for m in marks:
            cx, cy = m["center"]
            # The centre pixel must actually be the white dot, or the
            # "known position" the whole calibration rests on is a guess.
            off = (int(cy) * w + int(cx)) * 3
            assert rgb[off:off + 3] == bytes((255, 255, 255)), m

    def test_markers_stay_inside_a_small_image(self):
        rgb, marks = screen_mod.draw_markers(bytes(40 * 40 * 3), 40, 40)
        assert len(rgb) == 40 * 40 * 3
        for m in marks:
            cx, cy = m["center"]
            assert 0 <= cx < 40 and 0 <= cy < 40, m

    def test_the_two_markers_do_not_share_a_position(self):
        """Two markers at the same place cannot determine a scale, and the
        fit would divide by zero."""
        _, marks = screen_mod.draw_markers(bytes(200 * 200 * 3), 200, 200)
        assert marks[0]["center"] != marks[1]["center"]
