**English** | [简体中文](README.zh-CN.md)

# dsh-clawtouch

> **Tell it what to click. It clicks it — with a real mouse.**
> A [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) plugin
> that gives an agent a physical USB HID mouse and keyboard, driven by plain
> language instead of coordinates.

```
computer_click({ target: "the blue Send button at the bottom right" })
→ clicked (6299, 686) in window "WeChat" — Send button (confidence 0.95)
```

The agent never sees a screenshot, never computes a coordinate, and never
learns what resolution anything is.

---

## What it actually does

One `computer_click` call runs five steps inside the plugin:

| # | Step | Why it cannot be skipped |
|---|------|--------------------------|
| 1 | Crop to the target window | A 5120px-wide desktop sent whole located **0 of 6** targets in testing; cropped, **6 of 6**. |
| 2 | Bound the width (default 1600px) | The vision model rescales to its own budget anyway — send it detail it can keep. |
| 3 | Stamp two calibration markers | Their positions in the image are known exactly. |
| 4 | **One** vision call: both markers *and* the target | Calibration must describe the *same* rendering the target was found in. |
| 5 | Fit `model = scale × image + offset`, invert, click | Recovers the model's unreported internal rescale. |

Step 3 is what makes this work on surfaces with no accessibility tree —
Electron apps that expose four elements, self-drawn UI, games,
remote-desktop sessions. Anything can be drawn on; not everything can be
queried.

Step 1 has a catch worth knowing about: capturing a window's rectangle
captures whatever is **in front of** it. A vision model asked about a
covered window fluently describes the app on top, and that answer is
indistinguishable from a right one.

So a window that is not in front is **brought forward first** — by
clicking its title bar with the real mouse, exactly as a person would, not
through a focus-stealing API. The point is chosen by asking the
application itself (`WM_NCHITTEST` answering `HTCAPTION`): a spot it calls
a drag area. "The top strip is the title bar" would not be safe enough —
in a browser that strip is the tabs.

Even that answer is necessary rather than sufficient: Chrome reports its
"new tab" button as a drag area, and clicking it opens a tab. So the
caption is scanned from the right, where the space just inside the window
controls is least likely to be anything but drag area. It stays best
effort, and the re-read after the click is what makes that safe: you are
told if it cannot be read back, if what came back is a different window
(same title, and a same-application sibling counts — the rectangle has to
match too, since raising does not move a window), and if it came back
still saying it is not in front, which means the raise did not take and
the next click may be swallowed as activation.

It gives up and says so when it cannot: a window with no caption to grab,
or one a modal dialog has disabled. Set `autoRaise: false` where the agent
must never change which window the person is looking at.

### Does it actually work?

Five consecutive clicks, each located from a plain-language description by
`qwen-vl-max`, driving a real calculator through a real USB HID device:

```
"the C button that clears the entry" -> clicked (294, 302)
"the 7 key"                          -> clicked (138, 407)
"the plus key"                       -> clicked (355, 507)
"the 9 key"                          -> clicked (287, 407)
"the equals button"                  -> clicked (361, 565)
```

The calculator showed **16**. On a separate six-target pass every target
landed inside the correct button (cells 79x53 px; largest error 13 px,
median about 7).

Dense UI holds up better, not worse. Five targets in a WeChat window
— the search field, a round icon button, two 24 px icons in the bottom
toolbar, the window's minimize button — located to within 7 px, the
tightest of them to **1 px**:

| target | error |
|---|---|
| search field | 2.8 px |
| round + button | 1.1 px |
| emoji icon (24 px) | 6.6 px |
| microphone icon (24 px) | 5.8 px |
| minimize button | 4.0 px |

That run is also where the calibration visibly earned its place: the model
rescaled the image to **0.90**, so an uncorrected coordinate near the
bottom of the window would have missed by around 90 px. The calculator
runs never showed this because the model happened to answer at 1.0 there
— which is exactly why the scale is measured every time instead of
assumed once.

### Where it works

| | window list | crop to a window | auto-raise | modal / occlusion guards |
|---|---|---|---|---|
| **Windows** | yes | yes | yes | yes |
| **macOS** | with `clawtouch-mcp[window]` (pyobjc) | yes | **no** | **no** |
| **Linux** | no | only with an explicit `region` | no | no |

Windows is where this has been used and measured. macOS lists windows
through Quartz and can crop to them, but has no equivalent of the caption
probe or the occlusion sampler yet, so a background window is worked with
as-is rather than brought forward. On Linux there is no window listing at
all, and without a `region` the capture is the whole desktop — the case
that located **0 of 6** targets in testing. Treat Linux as unsupported
until that is fixed.

The degradation is deliberate in one respect: a missing measurement is
never read as "fine". Where a guard cannot run it simply is not applied,
and nothing claims it was.

### Why hardware HID

Input arrives through the OS's standard USB HID driver stack, exactly like
a plugged-in keyboard, so the input side of the target needs no driver and
no agent process. See
[clawtouch-mcp](https://github.com/tinqiao-oss/clawtouch-mcp) for the
plumbing this builds on.

### Why a plugin and not just the MCP server

`clawtouch-mcp` is deliberately raw HID plumbing — no LLM, no agent loop.
Three things therefore cannot live in it:

- **A second model.** The model that runs a tool loop well and the model
  that points at pixels accurately are not the same model today. This layer
  routes "where is it" to a vision model and keeps the answer — not the
  image — in the agent's context.
- **A skill**, registered at runtime, so the workflow is present because
  the plugin loaded, not because a file landed in the right one of dsh's
  five skill roots.
- **A guard.** `ctx.tools.guard()` is synchronous and monotonic: returning
  a reason denies the call and nothing later can undo it. That is what lets
  Cmd+Q be *blocked* rather than warned about — a real HID keystroke lands
  on whatever window has focus, and if that is the agent's own window, the
  session ends mid-task.

---

## Install

```bash
pip install 'clawtouch-mcp[screenshot,window]>=0.5.0'   # HID, capture, window list
dsh plugin --profile <your-profile> add dsh-clawtouch
export DASHSCOPE_API_KEY=sk-...               # the vision model's key
```

Both extras, not just `[screenshot]`: this plugin crops to a window
before it looks at anything, and on macOS the window list is pyobjc,
which lives in `[window]`. Without it a call that names a window is
refused outright, and one that does not widens to the whole desktop —
the case the cropping exists to avoid. On Windows `[window]` installs
nothing; asking for it costs nothing either.

`@deepseek-ai/dsh-tools` comes from the host and is not declared as a
peer dependency on purpose: while dsh ships release candidates, no semver
range can match an installed `0.1.x-rc.y`, so declaring it would make the
install fail against the very version that works. Any dsh that can load
plugins already provides it.

You also need the hardware: a Raspberry Pi Pico 2 (about ¥55 / $8) running
the open [ClawTouch HID firmware](https://github.com/tinqiao-oss/clawtouch-hid),
or any turnkey [ClawTouch device](https://clawtouch.cn). Every click and
keystroke this plugin makes goes out through that board, so without one it
can look but cannot press anything. Looking is still worth checking first:
`dryRun: true` locates and reports without pressing anything, and needs no
board. (`mock: true` is for testing the plugin's link to `clawtouch-mcp`; it
presses nothing either, and every result says so.)

Verify before involving an agent:

```bash
node probe.js --windows       # can it see your windows?
node probe.js --move-test     # does the cursor land where the maths says?
```

`--move-test` needs **no API key**: it drives the real cursor to each
marker's own screen position and reports the residual. A few pixels is
normal. Tens of pixels means the problem is below the vision layer, and no
amount of prompt tuning will fix the clicks.

---

## Configuration

Everything is optional except a vision key.

```yaml
- insert:
    - id: clawtouch
      name: dsh-clawtouch
      config:
        command: clawtouch-mcp     # path to the executable
        args: []                   # --allow-screenshot is added for you
        port: COM6                 # serial port; omit to auto-detect
        maxWidth: 1600             # widest image sent to the vision model
        imageFormat: jpeg          # or png, for pixel-exact work
        dryRun: false              # locate and report, never press
        autoRaise: true            # click a background window's title bar
                                   # to bring it forward before looking
        allowQuitCombos: false     # see "Safety"
        allowFocusSwitchCombos: false  # see "Safety"
        registerSkill: true
        mock: false                # no device: exercises the server, presses
                                   # nothing, and every result says so
        vision:
          model: qwen-vl-max
          endpoint: ...            # any OpenAI-compatible vision endpoint
          apiKey: ...              # prefer DASHSCOPE_API_KEY in the env
          timeoutMs: 60000
```

`config` is **replaced wholesale** by a profile's own patch layer, not deep
merged — restate every key you want when overriding one.

### Choosing the vision model

This is the single highest-leverage setting, and newer is not better. In
testing on the same task, `qwen-vl-max` located targets to 3–4px after
calibration, while several newer and more expensive vision models scored
**0 of 12** even with calibration applied. Change it only with a
measurement, and `probe.js` is how you measure.

### Multi-monitor

`clawtouch-mcp` clamps clicks to `--screen`, which defaults to the
**primary** display, so a window on a second monitor is unreachable until
you declare the whole virtual desktop:

```yaml
args: ['--screen', '7680x1440']
```

That works for a display placed to the **right of or below** the primary
one. It cannot work for one placed to the **left or above**: `--screen`
carries a size and no origin, so the addressable area always starts at
`(0, 0)`, and such a display sits behind negative coordinates that no
`WxH` describes. Measured on macOS with a second display at origin
`(-1920, 0)`: `--screen 3432x1200` — the size of the whole virtual
desktop — still addresses only `[0,3432)x[0,1200)`, and every point on
that display clamps to `x=0`. Captures of it are fine; only the clicking
is out of range. Move it right of or below the primary in the OS display
settings, then widen `--screen` to include where it lands.

A clamped click is reported as such and this plugin turns it into an
error — a click that lands hundreds of pixels away is not a rounding
error.

---

## Tools

| Tool | What it does |
|------|--------------|
| `computer_click` | Click something described in words. |
| `computer_click_sequence` | Click several things in order, from **one** look. About twice as fast when every target is on screen at once and clicking them does not move the others. |
| `computer_find` | Locate without clicking — verify before acting. |
| `computer_windows` | List visible windows and their titles. |
| `computer_type` | Type text; pass `target` to click the field first. |
| `computer_key` | Press a key or combination. |
| `computer_scroll` | Scroll the wheel. |

## Safety

This gives an agent the same reach as a person at the keyboard, with no
undo.

- **Quit combos are blocked by default.** On macOS Cmd+Q and Cmd+W; on
  Windows and Linux desktops Alt+F4, Ctrl+W and Ctrl+F4. On a machine
  shared with the agent they would close the session (a browser showing the
  dsh web UI closes on Ctrl+W); set `allowQuitCombos: true` only when the
  machine being driven is not the one running dsh.
- **Window-switching combos are blocked by default too.** On Windows and
  Linux desktops Alt+Tab, Alt+Esc, Ctrl+Esc, Ctrl+Alt+Del and every
  Windows/Super-key combination; on macOS Cmd+Tab, Cmd+Space, Cmd+\`,
  Cmd+H, Cmd+M, Cmd+Option+Esc, Ctrl+arrows and Ctrl+F2/F3 (ordinary Cmd
  shortcuts such as Cmd+C still work). A real keystroke goes wherever focus
  is, so once focus leaves the task window every later key follows it —
  observed in testing, where an agent pressed Alt+Tab and the rest of the
  run never got back. To work in another window on Windows, pass `window`
  to `computer_click`, which brings it forward by its title bar; elsewhere
  click a visible part of it. Set `allowFocusSwitchCombos: true` only when
  the machine being driven is not the one running dsh.
- Both guards read a combination the way `clawtouch-mcp` will parse it:
  `key: "alt+tab"` is Alt+Tab, surrounding whitespace is stripped as Python
  strips it, and a numeric key is read as its digits.
- **`dryRun: true`** locates and reports without pressing anything — not
  even the title-bar click that brings a background window forward, so a
  covered window is refused instead of raised. Worth a first pass on a new
  machine, and it needs no board.
- **Text is typed on a US keyboard layout**, one key per character.
  `computer_type` refuses text containing anything outside plain ASCII
  (Chinese, emoji, curly quotes) before it types or clicks anything, rather
  than stopping halfway through it.
- **A "not found" answer is honest, not transient.** The plugin refuses to
  click rather than guessing, and refuses a calibration whose two axes
  disagree — a misread marker would put the click anywhere.

## Development

```bash
node test.js     # coordinate maths and reply parsing
node smoke.js    # mounts the plugin against dsh's real schema compiler
```

`smoke.js` needs `@deepseek-ai/dsh-tools` resolvable. Neither test needs
hardware or a key.

## License

MIT — see [LICENSE](../../../LICENSE).
