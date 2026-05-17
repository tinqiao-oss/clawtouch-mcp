# macOS Setup Guide

Verified on macOS 26.3.1 (arm64) with a Raspberry Pi Pico 2 running
ClawTouch HID firmware v1.0. Most of the gotchas below apply to all
macOS versions ≥ 12; the few that don't are flagged inline.

## Prerequisites

| | Notes |
|---|---|
| **macOS 12 or later** | Tested on 26.3.1 (Tahoe-era) on Apple Silicon |
| **Python 3.10+** | Apple ships 3.9 — too old. Install via [uv](#install-python-via-uv-recommended) (fast, isolated) or Homebrew |
| **Xcode Command Line Tools** | `xcode-select --install` if `gcc` is missing — pyserial needs the build toolchain |

### Install Python via `uv` (recommended)

`uv` is a one-binary Python installer that handles the whole venv +
package flow without a system-wide package manager:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

# Set up an isolated env with Python 3.12 (uv downloads it if needed)
uv venv --python 3.12 ~/clawtouch-test
source ~/clawtouch-test/bin/activate
```

Then install `clawtouch-mcp`:

```bash
uv pip install "clawtouch-mcp[screenshot]"
```

> **Note**: `uv venv` does NOT create a `pip` binary in the venv by
> default. Use `uv pip install ...` instead of `pip install ...`.

## First-time Pico hookup

### The "Keyboard Setup Assistant" dialog

The first time you plug a Pico into macOS you'll see:

> **键盘设置助理 / Keyboard Setup Assistant**
> 无法识别你的 Raspberry Pi 设备 / Your keyboard cannot be identified
> 如果你的键盘可以正常使用… 则可以退出此应用程序

**Click "Cancel" / "退出".** This dialog wants you to press Shift +
number keys so it can determine your keyboard layout — but the Pico
is a remote-controlled HID device, not a physical keyboard you can
type on directly. Cancelling tells macOS to use the generic ANSI
layout, which is exactly what we want.

This only appears the first time. macOS remembers the device serial
after that.

### Pico shows up as TWO ports (this is normal)

The standard ClawTouch firmware enables a USB composite device with
two CDC channels:

```bash
$ ls /dev/cu.usbmodem*
/dev/cu.usbmodem21201      # REPL console (firmware print output)
/dev/cu.usbmodem21203      # protocol data channel (HID commands)
```

Both ports share the same VID, PID, and serial number. To find them:

```python
from clawtouch_mcp.bridge import list_pico_ports
for p in list_pico_ports():
    if p["likely_pico"]:
        print(p["device"], "is_data_port=" + str(p["is_data_port"]))
```

`auto_detect_port()` automatically picks the data channel (the
higher-numbered one) — this was fixed in **v0.2.1** after testing on a
fresh Apple Silicon Mac mini found the previous version returning the
REPL console silently. If you're on v0.2.0 you'll see `ping()` return
`False` without an error — upgrade to v0.2.1+.

### `/dev/cu.*` vs `/dev/tty.*`

For each USB-CDC device, macOS exposes two paths:

- **`/dev/cu.usbmodemXXXXX`** — "calling unit", for outgoing connections (what you want)
- **`/dev/tty.usbmodemXXXXX`** — for incoming, blocks until DCD line asserts

Always use `cu.*` with pyserial. `list_pico_ports()` only returns
`cu.*` entries because that's what pyserial's `comports()` reports
on macOS.

## Running tests

### Mock mode (no hardware)

```bash
clawtouch-mcp --mock --log-level INFO
```

Should print:

```
... clawtouch_mcp.server: starting in MOCK mode — hardware is not touched
```

…and accept JSON-RPC on stdin. Send Ctrl+C to exit.

### Real Pico

```bash
# Auto-detect the data port (recommended)
clawtouch-mcp --log-level INFO

# Or specify it explicitly
clawtouch-mcp --port /dev/cu.usbmodem21203 --log-level INFO
```

The port name's trailing digits (the `21203`) come from the Pico's
USB device address; they change every time you re-plug. Don't hard-
code them in scripts — use `auto_detect_port()`.

### End-to-end HID sanity check

This script confirms the data channel is alive and the HID stack works
end-to-end (mouse + keyboard + `cmd` modifier). It will move your
cursor and type into TextEdit, so close anything important first:

```python
import asyncio, subprocess
from clawtouch_mcp.bridge import SerialHidBridge, auto_detect_port

async def main():
    b = SerialHidBridge(auto_detect_port(), timeout=2.0)
    await b.connect()
    await b.mouse_move(500, 300, relative=False)        # cursor should jump
    subprocess.run(["open", "-a", "TextEdit"])
    await asyncio.sleep(2)
    await b.key_combo(["gui"], "n")                      # cmd+n
    await asyncio.sleep(1)
    await b.type_text("hello world from ClawTouch HID")
    await b.key_combo(["gui"], "a")                      # cmd+a
    await b.key_combo(["gui"], "c")                      # cmd+c
    await asyncio.sleep(0.5)
    print(subprocess.run(["pbpaste"], capture_output=True,
                          text=True).stdout)
    await b.release_all()
    await b.close()

asyncio.run(main())
```

Expected `pbpaste` output: `Hello world from ClawTouch HID` (note the
capital "H" — that's TextEdit's autocorrect, not a HID bug; see
below).

## Known macOS-specific behaviors

### TextEdit autocorrects the first letter

Default macOS TextEdit has **"Edit → Substitutions → Smart Quotes,
Dashes, Spelling, Symbols"** enabled. The most visible effect for
HID testing: typed `hello world` becomes `Hello world` (sentence-
start capitalization). To disable for accurate input verification:

- TextEdit → Edit → Spelling and Grammar → uncheck "Correct Spelling Automatically"
- TextEdit → Edit → Substitutions → uncheck all

Or use a less helpful editor (VS Code, BBEdit) for the test.

### `hid.screenshot` needs Screen Recording permission

The `[screenshot]` extra uses `mss`, which on macOS 14+ requires the
calling process to be granted **Screen Recording** permission:

1. Run `clawtouch-mcp --allow-screenshot` once
2. Call `hid.screenshot` from your MCP client — macOS prompts for permission
3. Open **System Settings → Privacy & Security → Screen Recording**
4. Enable the entry for the terminal app that launched `clawtouch-mcp`
   (Terminal.app, iTerm2, your IDE, etc.)
5. Restart the terminal app (the permission only takes effect on new
   processes)

Without this, `hid.screenshot` returns a black image — no error.

**SSH-launched processes**: a Python process launched via SSH inherits
Screen Recording permission from the SSH daemon's parent. If you grant
**Remote Login** (or the terminal that exported the daemon) the
Screen Recording permission once, subsequent SSH-spawned `mss` calls
work without re-prompting. This was confirmed on macOS 26.3.1 / Apple
Silicon: a fresh `uv`-installed Python 3.12 called over SSH returned a
valid screenshot without any permission dialog.

### Input source matters for `hid.type`

The Pico sends raw HID keycodes (US ANSI layout). The system input
source translates those keycodes to characters. **Behavior under
Pinyin / 拼音 IME is more nuanced than "everything gets garbled"** —
verified empirically on macOS 26.3.1 with the system Pinyin IME:

| Input | What hits the document |
|-------|------------------------|
| `hid.type("hello")` + Enter | `hello` — Pinyin can't form a valid Chinese candidate from `hello`, so Enter commits the raw letters |
| `hid.type("ni hao")` + Enter | `你好` (or similar) — IME picks the first Chinese candidate that matches the pinyin |
| `hid.type("pinyin_test: ")` | `pinyin_test： ` — **the ASCII colon `:` silently becomes the fullwidth Chinese colon `：`** because Pinyin's "Chinese punctuation" toggle is on by default |
| `hid.key` (navigation, modifiers) | Unaffected — keycodes bypass the IME composition buffer |

**Implications for tests:**

- **Don't** rely on accuracy-sensitive ASCII tests (e.g. comparing
  pasted text to an expected string) while Pinyin is the active
  input source — punctuation will silently differ.
- **Do** switch to ABC (or any non-IME source) before any
  `hid.type` test that includes punctuation. `⌃+Space` then type
  `ABC` in the floating switcher.
- `hid.key` is layout-immune; modifier shortcuts (`cmd+c`, etc.)
  work regardless of IME state.

Other layouts to be aware of:

- **AZERTY (French)** — number row sends digits with a Shift mask
  inverted from US, so `hid.type("123")` produces `&é"` on AZERTY.
- **Dvorak** — every letter maps to a different glyph than US.
- **Colemak** — same as Dvorak.

The Pico cannot detect the active input source. If your application
needs reliable typing, switch to ABC programmatically before calling
`hid.type`:

```bash
# Activate ABC input source (macOS only, requires the keyboard menu
# to be visible in the menu bar — System Settings → Keyboard → Input
# Sources → "Show Input menu in menu bar")
osascript -e 'tell application "System Events" to keystroke space using {control down}'
```

For automated testing, switch to ABC before sending characters with
`hid.type`. Use `hid.key` (which sends named keys regardless of
layout) for navigation and shortcuts.

### macOS modifier aliases all work

`hid.key` accepts `cmd`, `win`, and `gui` as interchangeable names for
the Command (⌘) modifier. On macOS, all three trigger ⌘. Tested
combos:

| Combo | Behavior |
|-------|----------|
| `cmd+a` | Select all |
| `cmd+c` / `cmd+v` / `cmd+x` | Clipboard |
| `cmd+n` | New (in active app) |
| `cmd+space` | Spotlight |
| `cmd+tab` | App switcher |
| `cmd+w` / `cmd+q` | Close window / Quit app |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `auto_detect_port()` returns `None` | Pico not plugged in, or VID mismatch | `ls /dev/cu.usbmodem*` — should show two ports |
| `ping()` returns `False` on v0.2.0 | Old dual-CDC detection bug | Upgrade to v0.2.1+ |
| `ping()` returns `False` on v0.2.1+ | Wrong firmware on Pico, or wrong port | Check that the higher-numbered port is open; try `--port` explicitly |
| `Permission denied: '/dev/cu.usbmodemXXXXX'` | Another process holds the port | `lsof /dev/cu.usbmodemXXXXX` to find it; common culprit is a previous `screen` session |
| `hid.screenshot` returns black image | Screen Recording permission not granted | See "needs Screen Recording permission" above |
| `hid.type` produces wrong characters | System input source isn't ABC | Switch to ABC: ⌘+Space then type "ABC" |
| Cursor doesn't move when calling `mouse_move` | Cursor coordinates clamped to `--screen WxH` | Set `--screen 1920x1080` (or your actual size) when launching |

## Compatibility matrix

| | Verified on | Notes |
|---|---|---|
| Apple Silicon (arm64) | macOS 26.3.1, Mac mini M-series | ✅ uv installs CPython 3.12 directly |
| Intel x86_64 | _not yet tested_ | Should work — same code paths |
| macOS 12-13 | _not yet tested_ | `mss` permission flow may differ pre-14 |
| Pico 2 (RP2350) | Yes | VID `0x2E8A`, PID `0x000B` enumerated correctly |
| Pico W (RP2040) | _not yet tested_ | Shares VID `0x2E8A` so detection works as-is. May need single-CDC fallback if its firmware enumerates only one port — handled by `list_pico_ports()` already. |
