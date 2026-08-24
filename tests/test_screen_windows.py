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
