# Testing this plugin on macOS

Written after a full pass on Windows, for whoever runs the same pass on a
Mac. Read the first section before running anything: several things are
**meant** to be unavailable here, and "fixing" them would be a regression.

## What macOS actually supports

| | Windows | macOS |
|---|---|---|
| Window list + rectangles | yes | yes (needs `pyobjc`) |
| Crop a capture to one window | yes | yes |
| Raise a background window | yes | **no** |
| Occlusion measurement (`visible_fraction`) | yes | **no** |
| Modal / input-state guard (`enabled`) | yes | **no** |

The last three are absent by design, not broken. `clawtouch_mcp/screen.py`
has a `_list_windows_darwin` that returns geometry only, and this is the
honest form: a guard that cannot run must be reported as not run, never as
passed. **Do not add fake defaults to make the fields appear.**

Practical consequence: on macOS the plugin works with a window as it finds
it. If the target is behind another window, it says so and stops, rather
than capturing whatever is in front and answering about that.

## Prerequisites, and how to check each one

Run these first and report exactly which ones fail — several tiers below
can still run without hardware or an API key.

```bash
# 1. the repo, up to date
git -C <repo> pull --ff-only && git -C <repo> log --oneline -1

# 2. clawtouch-mcp 0.5.0 (the plugin needs screen.windows + markers)
pip install 'clawtouch-mcp[screenshot]>=0.5.0'
python -c "import clawtouch_mcp; print(clawtouch_mcp.__version__)"

# 3. pyobjc — without it there is no window list at all
python -c "import Quartz; print('Quartz ok')"

# 4. node
node --version          # >= 18

# 5. HID hardware (optional; Tier 3 only)
clawtouch-mcp --list-ports 2>/dev/null || python -c "
from clawtouch_mcp.bridge import list_pico_ports; print(list_pico_ports())"

# 6. vision key (optional; Tier 2 and 3)
echo "${DASHSCOPE_API_KEY:+key present}"
```

**Screen Recording permission**: the first capture will either fail or
return a black/desktop-only image until the terminal (or whatever runs
node) is granted Screen Recording in System Settings → Privacy & Security.
A black capture is this, not a bug in the plugin. Grant it, then **restart
the terminal app** — the permission is only picked up on relaunch.

## Tier 1 — no hardware, no API key

```bash
cd <repo>/oss/clawtouch-mcp/adapters/dsh/plugin
node test.js      # pure logic: coordinate maths, reply parsing, guards
node smoke.js     # needs @deepseek-ai/dsh-tools resolvable; skip if absent
```

Expected: `52 passed, 0 failed`, and `smoke ok`. These are platform
independent — a failure here is a real bug, not a macOS limitation.

Then the window list, which is the first thing that touches macOS APIs:

```bash
python -c "
from clawtouch_mcp import screen
for w in screen.list_windows():
    print(w['title'][:40], w['rect'],
          'vf=', w.get('visible_fraction'), 'raise=', w.get('raise_point'))"
```

Expected on macOS: real titles and rectangles, and **`vf= None`
`raise= None` on every line**. Both being absent is the correct result
here. If either shows a number, that is the bug this release fixed —
report it.

## Tier 2 — with an API key, no hardware

`probe.js` without `--click` locates but never moves the mouse.

```bash
export DASHSCOPE_API_KEY=...
node probe.js --windows
node probe.js --shot --window Safari      # writes a jpg, prints markers
node probe.js "the address bar" --window Safari
```

Check in order:

1. `--windows` lists windows. A window on a second display needs
   `--screen <total virtual desktop, e.g. 5120x1440>`, or its coordinates
   are clamped to the primary display.
2. `--shot` prints `markers:` and saves a file. **Open that file.** The two
   calibration markers must be visible at opposite corners, and the image
   must be the window you named — not the desktop, not the window in front
   of it.
3. The locate step prints `scale`. On a Retina display this is where
   HiDPI errors would show up: if the reported point is roughly half or
   double where the target actually is, the marker calibration is not
   doing its job and that is worth reporting with the numbers.

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
```

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
  console.log(JSON.stringify(value.windows[0], null, 2))
  console.log(t.output.render({}, value)[0].text.split('\n')[0])
})"
```

Expected on macOS:

- the object has `title`, `foreground`, `width`, `height` and **neither
  `visible_percent` nor `accepts_input`**;
- the rendered line ends with `— input state and occlusion NOT measured here`.

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

## Reporting

State plainly what ran, what did not, and why. If a tier was skipped for a
missing prerequisite, say which one. Do not describe an untested tier as
passing, and do not treat the three unsupported capabilities above as
failures — they are the documented shape of macOS support.
