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
`qwen-vl-max` (the default at the time, 2026-08), driving a real calculator
through a real USB HID device:

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

### dsh versions and dsh's own Computer Use

0.2.0 needs dsh 0.2.x: the eye goes through dsh's own model and attachment
services. Verified on dsh 0.2.2-rc.1 (2026-11-14): the tools work through
the native executor and through PTC (`run_code` calling `tools.computer_*`,
which get structured results), and the key guard also stops calls made from
inside a PTC program. dsh also ships an experimental Computer Use of its own. Turn on
one or the other in a profile, not both: this plugin's key guard only sees
its own tools.

Firmware 1.2 of [clawtouch-hid](https://github.com/tinqiao-oss/clawtouch-hid)
adds an absolute pointer, and on Windows `clawtouch-mcp` then puts the
cursor on a primary-monitor pixel in one report instead of several relative
nudges. Older firmware keeps working the old way.

### Why hardware HID

Input arrives through the OS's standard USB HID driver stack, exactly like
a plugged-in keyboard, so the input side of the target needs no driver and
no agent process. See
[clawtouch-mcp](https://github.com/tinqiao-oss/clawtouch-mcp) for the
plumbing this builds on.

### Why a plugin and not just the MCP server

`clawtouch-mcp` is deliberately raw HID plumbing — no LLM, no agent loop.
Three things therefore cannot live in it:

- **A separate look.** "Where is it" is answered inside the tool, by a
  model you choose in dsh (by default the conversation's own), and only the
  answer — not the image — enters the agent's context.
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
dsh plugin --profile <your-profile> add dsh-clawtouch
pip install 'clawtouch-mcp[screenshot,window]>=0.6.1'   # HID, capture, window list
```

The second line can also be done from the plugin's settings page (below):
with Python 3.10+ on the machine, its **Install** button puts clawtouch-mcp
into a folder of its own under the dsh home and sets the path.

No API key goes to this plugin. The model that looks at the screen is one
you configure in dsh — by default the one the conversation is using — see
[Choosing the eye](#choosing-the-eye).

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
the open [ClawTouch HID firmware](https://github.com/tinqiao-oss/clawtouch-hid).
Every click and
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

### The settings page

In dsh, open **Plugins → dsh-clawtouch**. Below the description:

- **Status**, checked when the page opens and again on request, pressing
  nothing: which model looks at the screen (and, for a configured route,
  whether dsh says it accepts images), whether `clawtouch-mcp` runs and which
  version, whether the board answers, its firmware, and whether moves use the
  absolute pointer. The board check needs `clawtouch-mcp` 0.6.1 or later.
- **The eye**: the conversation's model, or any route in dsh that declares
  image input, with the measured hit rate beside the models we measured.
- **Look, don't press** (`dryRun`), and under **Advanced** the capture width
  (`maxWidth`) and the path to `clawtouch-mcp` (`command`).
- A leftover 0.1.x `vision.endpoint` / `apiKey` / `extraBody` is named (its
  value is never shown) with a button that removes exactly those keys.
- **Installing clawtouch-mcp**, when it cannot run or is older than the plugin
  needs. The page looks for Python 3.10+ (and for a clawtouch-mcp already
  installed, offered with "Use this"), and **Install** creates a venv at
  `<dsh home>/clawtouch-mcp/venv`, installs
  `clawtouch-mcp[screenshot,window]` at the version the plugin was tested with,
  checks it, and saves its path. It downloads from PyPI, and from the Tsinghua
  mirror if PyPI fails or does not answer in time; pip runs isolated and reads
  no pip configuration file, so no setting of yours redirects it, and nothing
  outside that folder is touched. It goes online only when you click. It does not install
  Python itself: without one, the page says so and links python.org.

Saving reloads the plugin, so it is refused while the agent is in the middle
of a click or typing, and a call that starts while the save is written is
refused before anything is pressed. A save keeps every other key of the row
as it is. Everything below that the page does not show — the key guards
among them, on purpose — is set in YAML.

### In YAML

Everything is optional.

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
        vision:                    # the eye; leave out to use the
          provider: bailian        # conversation's own model. A dsh route
          model: qwen3.8-max       # name and model id, set both or neither
          effort: off              # reasoning level; default: "off" when
                                   # the route offers it, else its lowest
          timeoutMs: 60000
```

`config` is **replaced wholesale** by a profile's own patch layer, not deep
merged — restate every key you want when overriding one.

### Choosing the eye

The eye is the model that looks at each screenshot and says where the target
is. It is a model route you configure in dsh, not something this plugin
ships: with `vision` left out, the conversation's own model looks; with
`vision.provider` and `vision.model` set, that route does (the settings page
offers every route that declares image input). Credentials, gateways and
limits live in your dsh model settings.

The route must accept images. dsh would otherwise replace the screenshot
with a text note and the model could still answer with made-up coordinates,
so the plugin checks the route's declared input first and refuses — before
anything is pressed — when `image` is not declared.

**This is the setting that decides how often a click lands on the wrong
thing.** Measured on saved captures with this plugin's prompt and
calibration, offline, with the image delivered the way dsh delivers it
(dsh's note in front of the image simulated; `tools/vision-bench` in the
source repo, 2026-11-14; 165 looks per model over 3 runs; "absent" = 24
targets that were not on screen):

| Eye (through dsh) | Hit | Wrong click | Clicked an absent target | Median per look |
|---|---|---|---|---|
| `qwen3.8-max`, thinking off | 96.4% | 2.4% | 0 | 2.0 s |
| `qwen3-vl-plus` | 96.4% | 3.6% | 3 | 1.8 s |
| `deepseek-flash`, thinking off (dsh's default model) | 78.2% | 21.8% | 1 | 0.9 s |
| Claude, GPT, others | not measured | | | |

With DeepSeek as the conversation model and nothing configured, roughly one
click in five lands on something next to the target. Every result line names
the eye that looked (`eye provider/model`), so this is visible. When several
targets are asked for in one look (`computer_click_sequence`), `qwen3.8-max`
more often answers "not found" than guesses (87% of targets found, 1.8%
wrong) — the sequence is then refused rather than half-clicked.
End-to-end on a real board through dsh 0.2.2-rc.1 (DeepSeek driving,
calculator 7+9 and Paint pencil + red, each run checked through UI
Automation afterwards; small samples — the table is the measurement):
`qwen3.8-max` 4 of 5 (in the miss, another application covered the task
window entirely; the plugin refused to work on it), `qwen3-vl-plus` 4 of 4,
`deepseek-flash` 3 of 5.

Why "the way dsh delivers the image" matters: dsh puts a line in front of
every image that states its pixel size, and with it `qwen3.8-max` mixed
coordinate frames and dropped from 98% to 64% on the earlier prompt. Since
0.2.0 the prompt pins one frame (0–1000) for the calibration markers and
the targets alike, which brought it back.

A Bailian (DashScope) route for `dsh-llm-pi-ai`, ready to paste into your
profile's `cordis.patch.yml`. The two `compat` lines are required: without
them DashScope rejects the request, or thinking stays on and a single look
takes 45 s or more (measured).

```yaml
- id: llm-pi-ai
  config:
    providers:
      bailian:
        api: openai-completions
        baseURL: https://dashscope.aliyuncs.com/compatible-mode/v1
        apiKeyEnv: DASHSCOPE_API_KEY
        compat:
          thinkingFormat: qwen
          supportsDeveloperRole: false
        models:
          - id: qwen3.8-max
            input: [text, image]
            reasoningEfforts:
              off:
              high: high
- id: clawtouch
  config:
    vision:
      provider: bailian
      model: qwen3.8-max
```

Two costs to know about. Each look stores its screenshot as a dsh
attachment, and dsh keeps attachments forever: measured window captures are
20–70 KB, so a hundred clicks a day is a few megabytes a day. And the look
is a separate model call whose tokens do not show in the conversation's own
usage figures.

**Coming from 0.1.x:** `vision.endpoint`, `vision.apiKey` and
`vision.extraBody` are no longer read, and `DASHSCOPE_API_KEY` is not read by
the plugin at all. A config that still sets any of them is refused with
instructions (nothing is pressed) rather than quietly switching to a
different model. Remove them, then either leave `vision` out or point it at
a dsh route as above.

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
  Windows and Linux desktops Alt+F4, Ctrl+W, Ctrl+F4 and Alt+Space (the
  window menu, whose next key can close the window). On a machine
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
node test.js                # coordinate maths, reply parsing, guards, the page's routes
node --test client.test.js  # the settings page, rendered against a React stand-in
node smoke.js               # mounts the plugin against dsh's real schema compiler
```

`smoke.js` needs `@deepseek-ai/dsh-tools` resolvable. None of them needs
hardware or a key.

## License

MIT — see [LICENSE](../../../LICENSE).
