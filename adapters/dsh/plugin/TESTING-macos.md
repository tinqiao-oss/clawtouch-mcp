# Testing this plugin on macOS

Written after a full pass on Windows, then revised after a full pass on
macOS 26.5.2 (Apple Silicon, Retina primary + a second display, real HID
hardware, vision key). Read the first two sections before running
anything: several things are **meant** to be unavailable here, and one
thing that is not a limitation at all is the most likely to be reported
as a bug.

## What macOS actually supports

| | Windows | macOS |
|---|---|---|
| Window list + rectangles | yes | yes (needs `pyobjc`) |
| Which **application** is frontmost | yes | yes |
| Which **window** of it is frontmost | yes | best effort |
| Crop a capture to one window | yes | yes |
| Raise a background window | yes | **no** |
| Occlusion measurement (`visible_fraction`) | yes | **no** |
| Modal / input-state guard (`enabled`) | yes | **no** |
| A window on another Space / full-screen desktop | n/a | **not listed** |
| A minimised window | hidden unless `include_offscreen`, then `minimized: true` | hidden unless `include_offscreen`, and then **unlabelled** |

The three **no** rows are absent by design, not broken.
`clawtouch_mcp/screen.py` has a `_list_windows_darwin` that returns
geometry, plus a frontmost answer where the OS could be asked for one,
and nothing else. That is the honest form: a guard that cannot run must
be reported as not run, never as passed — which is also why `foreground`
itself goes **missing** rather than turning into `false` on the rare
occasion the frontmost query cannot be answered. **Do not add fake
defaults to make any of these fields appear.**

**What that costs you, stated plainly.** Because `visible_fraction` is
absent, the occlusion guard cannot run — and a guard that cannot run does
not refuse. `assertOnTop` lets an unmeasured window through on purpose
(refusing would make the plugin unusable on this platform), so a target
sitting behind another window **is captured, and acted on as if it were
on top**. Whether a click follows depends on the rest of the run — the
model still has to return a target and the point still has to be
in-bounds — but if one does, it lands on the window in front, not on the
one you named. What you get instead is a suffix on every answer:
`(input state and occlusion unmeasured here)`. That suffix is the entire
warning. Measured here: with a Finder window laid over Calculator,
`--shot --window Calculator` returned an image that was more than half
Finder sidebar, and said only that occlusion was unmeasured. So on macOS,
put the target on top yourself before trusting any answer about it, and
do not read the suffix as "checked, and it is fine".

## Spaces: the thing that will look like a bug and is not

macOS lists only what is **on screen right now**, and a Space that is not
showing is not on screen. Every full-screen application owns a Space of
its own, so while your editor is full-screen, everything that lives on
the desktop behind it is missing from the list. With two displays each
showing its own Space you get both of those Spaces at once, not one — so
the list is "whatever is visible", not "one desktop". The plugin reports
this accurately and unhelpfully:

```
no visible window matching "WeChat". Visible windows: "Code", "ClawScience"
```

which reads as "WeChat is not running". It is running; it is one swipe
away. Measured here: with VS Code full-screen and nothing else visible,
`list_windows()` returned 2 entries — both VS Code's — while the same
call with `include_offscreen=True` returned 90.

Before any tier below, **put the target application on the Space you are
actually looking at**, and keep it there. Related traps, all measured:

- Moving a window to a display whose active Space is a full-screen app
  puts it on that display's *other* Space, where it disappears from the
  list — and `open -a` does not bring it back.
- A minimised window vanishes from the default list — which is what the
  plugin always asks for, so for its purposes it is simply gone. Both
  platforms hide it there; the difference is that `include_offscreen`
  brings it back **labelled** `minimized: true` on Windows and
  **unlabelled** on macOS, which does not answer that question at all.
- `probe.js --windows` prints one line per visible window and nothing
  else, so when the only thing visible is a full-screen app you get a
  one-line answer, and when nothing at all qualifies you get no output
  rather than "the list was empty".

None of this is the plugin inventing an answer, which is why it is here
and not in the bug list. It is the shape of the platform.

## Prerequisites, and how to check each one

Run these first and report exactly which ones fail — several tiers below
can still run without hardware or an API key.

```bash
# 1. the repo, up to date
git -C <repo> pull --ff-only && git -C <repo> log --oneline -1

# 2. clawtouch-mcp from THIS CHECKOUT, with BOTH extras. `[screenshot]`
#    is mss + Pillow; the window list is pyobjc and lives in `[window]`,
#    and installing only `[screenshot]` makes step 3 below fail.
#    Editable and local on purpose: `pip install clawtouch-mcp` fetches
#    the published wheel, and the version number does not move until a
#    release — so PyPI can hand you an older implementation while this
#    guide describes the current one, and you would be testing neither.
python3 -m pip install -e '<repo>/oss/clawtouch-mcp[screenshot,window]'
python3 -c "import clawtouch_mcp; print(clawtouch_mcp.__file__)"

# 3. pyobjc — without it there is no window list at all
python3 -c "import Quartz; print('Quartz ok')"

# 4. node
node --version          # >= 18

# 5. HID hardware (optional; Tier 1.5 and 3)
python3 -c "from clawtouch_mcp.bridge import list_pico_ports
print([p['device'] for p in list_pico_ports() if p['likely_pico']])"

# 6. vision key (optional; Tier 2 and 3)
echo "${DASHSCOPE_API_KEY:+key present}"
```

Print the `__file__`, not the version: it is the only way to see which
copy you got. `probe.js` spawns whatever `clawtouch-mcp` is first on
`PATH`, so activate the environment you installed into — or pass
`--command /path/to/venv/bin/clawtouch-mcp` — before believing any result
below.

`python3`, not `python`: a stock Mac has no `python` on PATH at all, and
the Command Line Tools `python3` can be too old for this package's
`requires-python` (3.9.6 on the machine this was written on, against a
floor of 3.10). Use a 3.10+ interpreter — a venv is the easy way.

**Screen Recording permission**: the first capture will either fail or
return a black/desktop-only image until the terminal (or whatever runs
node) is granted Screen Recording in System Settings → Privacy & Security.
A black capture is this, not a bug in the plugin. Grant it, then **restart
the terminal app** — the permission is only picked up on relaunch. The
same permission also controls window *titles*: without it macOS still
reports rectangles but blanks the names, and the list falls back to
application names.

## Tier 1 — no hardware, no API key

```bash
cd <repo>/oss/clawtouch-mcp/adapters/dsh/plugin
node test.js      # pure logic: coordinate maths, reply parsing, guards
npm install --no-save @deepseek-ai/dsh-tools   # smoke.js only
node smoke.js     # registers against the real dsh tool schema
```

Expected: `77 passed, 0 failed`, and `smoke ok`. These are platform
independent — a failure here is a real bug, not a macOS limitation.
(`--no-save` on purpose: the published plugin has no dependencies and
smoke.js is a dev-only harness, so this must not land in `package.json`.
Nothing to clean up afterwards either — with `--no-save` npm keeps the
lockfile inside `node_modules/`, which is already git-ignored.)

Then the window list, which is the first thing that touches macOS APIs:

```bash
python3 -c "
from clawtouch_mcp import screen
for w in screen.list_windows():
    print(w.get('foreground') and '*' or ' ', w['title'][:40], w['rect'],
          'vf=', w.get('visible_fraction'), 'raise=', w.get('raise_point'))"
```

Expected on macOS: real titles and rectangles, **`vf= None`
`raise= None` on every line**, and the `*` on **at most one** window,
belonging to the application you are actually in. Both `vf` and `raise`
being absent is the correct result here; if either shows a number, report
it.

Read the `*` for exactly what it is, because half of it is measured and
half of it is not. Which **application** is frontmost is a real query
(`NSWorkspace.frontmostApplication`, matched by pid), and that half is
solid: the `*` must never land on a window belonging to some other
application, and never on more than one window. Which **window of that
application** is a heuristic — the first on-screen one macOS gave a
title, because the untitled entries are its service windows. That
separated the strips from the real window in every case measured, but it
is not a guarantee, so a `*` on the wrong window *of the right app* is
worth reporting as a note rather than as a blocker.

No `*` at all is a valid answer, and there are two different reasons for
it. If the field is **present and `false` everywhere**, the OS was asked
and said none of these is in front — measured causes: the screen is
locked (macOS answers loginwindow, which owns no listed window), or the
frontmost application's windows are minimised or on another Space. If the
field is **missing entirely**, the query itself failed; that is the only
case where it should be absent, and everything downstream says
`which is frontmost unmeasured here`.

## Tier 1.5 — with the HID device, still no API key

Do this before touching the vision layer. It drives the real cursor to
each calibration marker's own screen position and reports how far off it
landed, which tests the serial link, the firmware, the OS cursor query
and the convergence loop — everything under the model:

```bash
node probe.js --move-test --window Calculator
```

`--window` matches against the window title and the owning application's
name as macOS reports them, and those **may be localised**. On a
Chinese-language Mac the Calculator answers to `计算器` and not to
`Calculator` — measured: `--window Calculator` replies `no visible window
matching "Calculator". Visible windows: "计算器"`, which reads like the
app is closed. Copy what `--windows` printed rather than typing the
English name.

One more reason to copy it exactly: an application's untitled service
windows are listed under the application's *name*, so a bare app name can
match one of those instead of the window you meant — the `*` is chosen by
a real query, but `--window` is a substring match and does not consult
it. If `--shot` hands you a strip a few dozen pixels tall, that is what
happened; use the window's own title.

Expected: `identity fit: x=1.000000 y=1.000000` and `OK — worst residual
Npx`. Measured here: 1–5 px. A residual in the tens of pixels means
something below the model is wrong — the coordinate spaces disagreeing is
the usual cause, but competing input, a dead device or a UI dead zone
look the same from here, so read the `hint` line before concluding.

**What this does not prove.** The target it drives to is computed from
the capture's own `capture_rect` and `image_scale`, and then compared
against that same computed point — so a capture whose geometry is wrong
would still report `OK`, with the cursor arriving neatly at the wrong
place. The `identity fit` line is likewise a check of the arithmetic
against itself (marker centres fitted to marker centres), not of the
capture. For the capture geometry, use `--shot` and compare its
`WxH (scale …)` against the window's own size from `--windows`.

Two measured notes for whoever compares numbers:

- The convergence loop stops as soon as both axes are within
  `MOVE_TOLERANCE` (5 px), so on a successful unclamped move the number
  `probe.js` prints — the larger of the two axes — is at most 5 by
  construction. A `PROBLEM` line therefore means clamped, not converged,
  or the tool call itself failed; it never means "slightly imprecise".
- The declared screen size tracked with the result near a display seam:
  same window, same targets, `--screen 1512x982` settled 1 px off and
  `--screen 3432x1200` settled 4 px off, 6 interleaved runs of 6. Both
  are legitimate early exits inside tolerance and neither misses a
  target. Recorded so the next person seeing 4 px knows it has been seen
  before — not as a demonstration that the declared size caused it, which
  would need the starting cursor position and the carried-over gain
  estimate controlled, and they were not.

## Tier 2 — with an API key, no hardware

`probe.js` without `--click` locates but never moves the mouse.

```bash
export DASHSCOPE_API_KEY=...
node probe.js --windows
node probe.js --shot --window Calculator   # writes a jpg, prints markers
node probe.js "the 7 key" --window Calculator
```

Check in order:

1. `--windows` lists windows. Remember Spaces: what is missing is
   probably on another desktop, not absent. A window on a second display
   needs `--screen <total virtual desktop, e.g. 3432x1200>`, or its
   coordinates are clamped to the primary display.

   **Which side the display is on decides whether that works at all.**
   `--screen` takes a size and no origin, and macOS puts the primary
   display's corner at `(0, 0)`, so a display to the right or below is
   reachable by widening `--screen` and a display to the **left or
   above** is not — its coordinates are negative and no `WxH` describes
   them. Both arrangements were measured here. With the second display
   moved to origin `(-1920, 0)`, a Calculator window at
   `[-1000, 400, -770, 808]` **captures perfectly** (`230x408`, markers
   correct — the capture path has no such limit) while every point on it
   clamps to `x=0`, `--screen 3432x1200` included. The tools say so
   rather than clicking there: `--move-test` reports `CLAMPED` with a
   980 px residual, and `--click` refuses before sending anything. If
   this is your arrangement, move that display right of the primary in
   System Settings → Displays; there is no flag for it.
2. `--shot` prints `markers:` and saves a file. **Open that file.** The
   two calibration markers must be visible at opposite corners, and the
   image must be the window you named — not the desktop, and (see the
   occlusion note above) check with your own eyes that nothing is sitting
   in front of it, because nothing else will.
3. HiDPI errors show up in the **capture size**, and only `--shot`
   prints the scale alongside it (`230x408 from […] (scale 1.0000,
   1.0000)`); the locate step prints the rectangle and the size but not
   the scale. Compare that size against the window's own size from
   `--windows`: on this Retina Mac they match, because the capture comes
   back at logical point size — a 230x408-point window is a 230x408
   image. A capture at twice the window's point size is the HiDPI bug.
   The `fit` line that locate does print will **not** show it: that
   number measures the vision model's own internal resize and sits near 1
   either way. A model that normalises each axis independently
   legitimately returns two different scales (measured: `x=0.9526
   y=1.0353`), and the calibration allows that on purpose.

## Tier 3 — with the HID device

Use a harmless target. **Calculator is the standard one**: digits only,
`Escape` clears, nothing is saved. Do not use a text editor that has
restored a real file, and do not drive any messaging app's send button.

```bash
node probe.js "the 7 key" --window Calculator --click
```

Then, and this is the part that matters: **take an independent screenshot
and look at it.** The tool reporting `clicked: true` means a report was
sent and the cursor arrived, not that the right thing was pressed. Every
end-to-end claim in this project is backed by a screenshot taken
separately from the tool that made the claim.

```bash
screencapture -x /tmp/verify.png && open /tmp/verify.png
# or, for one window's rectangle only:
screencapture -x -R <x>,<y>,<w>,<h> /tmp/verify.png
```

Measured here: before the click Calculator read `0` above an `AC` key,
after it `7` above a `C` key — the `AC`→`C` change only happens once a
digit has really been entered, which is what makes the screenshot proof
rather than decoration.

**Second display.** If the target is on a second monitor and you have not
passed `--screen`, the click is **refused before anything is sent**:

```
(1954, 814) is outside the 1512x982 screen clawtouch-mcp can address, so
the click would land somewhere else entirely; nothing was sent. …
```

That is the correct behaviour and it was verified on hardware — no stray
click was emitted. Re-run with `--screen <whole virtual desktop>` and it
lands.

With the same display moved to the **left** of the primary, the refusal
stays but the remedy changes, and the message changes with it:

```
(-963, 611) is outside the 3432x1200 screen clawtouch-mcp can address, so
the click would land somewhere else entirely; nothing was sent. A negative
coordinate is out of range whatever --screen says: the flag carries a size
and no origin, so the addressable area always starts at (0, 0). If this is
a display placed left of or above the primary one, move it right of or
below in the OS display settings — and give --screen a size that includes
where it lands.
```

(That last clause matters: rearranging alone turns the coordinate positive
and lands it past bounds that were only ever the primary monitor, so the
second attempt fails too. A point that is negative on one axis *and* past
an edge on the other gets both halves.)

Saying "widen `--screen`" there would be advice that cannot work — the
caller does it, gets the identical failure, and concludes the tool is
broken. `--move-test` and the multi-target refusal carry the same split.

## The one behaviour worth checking closely

This release stopped the plugin from inventing measurements it never took.
On macOS that is directly visible:

```bash
node -e "
import('./index.js').then(async ({ apply }) => {
  const tools = new Map()
  apply({ logger: () => ({info(){},warn(){},error(){},debug(){}}), on(){},
          tools: { register: (d) => (tools.set(d.name, d), () => {}), guard: () => () => {} },
          inject: (_n, cb) => cb({ effect: (f) => f(), skills: { register: () => () => {} } }) },
        { vision: { apiKey: process.env.DASHSCOPE_API_KEY } })
  const t = tools.get('computer_windows')
  const value = await t.execute({}, {})
  const front = value.windows.find((w) => w.foreground) ?? value.windows[0]
  console.log(JSON.stringify(front, null, 2))
  console.log(t.output.render({}, value)[0].text)
})"
```

Expected on macOS:

- the object has `title`, `width`, `height`, normally also `foreground`,
  and **neither `visible_percent` nor `accepts_input`**;
- every rendered line ends with
  `— input state and occlusion NOT measured here`;
- **at most one** line carries the `*`, and it belongs to the application
  you are in — in practice the window you are looking at rather than a
  32-pixel-tall strip of the same application, though that last part is
  the heuristic described in Tier 1, not a guarantee;
- if `foreground` is **missing** rather than `false`, the frontmost query
  failed — the note then also says `which is frontmost unmeasured here`,
  and that is the honest shape, not a bug.

Measured on Windows for contrast, same code, same day:

```json
{ "title": "计算器", "foreground": true, "width": 322, "height": 534,
  "visible_percent": 100, "accepts_input": true }
```
```
* 计算器  (322x534)
```

Both fields present, and no note — because on Windows both guards
actually ran.

If macOS shows `visible_percent: 100` or `accepts_input: true`, the plugin
is claiming a guard ran when it did not — report it as a blocker.

## When something hangs or dies for no visible reason

- **A call that fails at almost exactly 30 s** is the MCP client's own
  per-call ceiling (`DEFAULT_CALL_TIMEOUT_MS` in `lib/mcp-client.js`); it
  is not configurable from the plugin config or from `probe.js`. It
  surfaces as `tools/call timed out after 30000ms`, or, if the process
  dies first, as a bare `clawtouch-mcp exited (code 1)` — neither of
  which says what was slow. Seen once on this Mac: four consecutive
  screen captures took 30.0 s each at the start of a session and were
  never slow again over 50+ later captures (median 36 ms), while the
  native `screencapture` and the raw CoreGraphics calls stayed fast
  throughout. Unexplained; if you hit it, say so, and time a bare
  `mss` grab to separate the capture from the plugin.
- **Nothing carries the `*`**, and every untargeted capture says
  `first listed window "…" (nothing reported itself as foreground)`.
  Two ordinary causes before suspecting a bug, both measured: the screen
  is **locked** — macOS then answers `loginwindow` as the frontmost
  application, and it owns no listed window — or the frontmost
  application's windows are on a Space you are not looking at. Both are
  correct answers, and the plugin still captures the first listed window;
  it just declines to call it the foreground one. Unlock, or bring the
  target's Space forward, and the `*` comes back.
- **The window list being unavailable is answered two different ways,**
  and which one you get depends on whether you named a window. With
  `--window X` it now **refuses** — nothing is captured, and the message
  says the listing could not be read, so "it is not there" is never
  claimed about a window nobody could look for. Without a window it falls
  back to a full-desktop capture, with a `[warn]` naming the cause and
  `captured full screen` in place of `captured window "X"`; there, the
  whole desktop is still an answer to the question that was asked. The
  usual cause of either is `pyobjc` missing — see prerequisite 2.

## Reporting

State plainly what ran, what did not, and why. If a tier was skipped for a
missing prerequisite, say which one. Do not describe an untested tier as
passing, and do not treat the unsupported capabilities above as
failures — they are the documented shape of macOS support.
