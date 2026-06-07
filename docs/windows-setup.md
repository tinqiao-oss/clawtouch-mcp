# Windows Setup Guide

Verified on Windows 11 (build 26100, x64) with a Raspberry Pi Pico 2
running ClawTouch HID firmware v1.0+ (setup is identical for v1.0 and
v1.1 — the v1.1 drag/hold-key tools don't change the install path),
driving the Claude Code VS Code extension over MCP stdio. Most of the
gotchas below apply to Windows 10 1809+ too; the few that don't are
flagged inline.

## Prerequisites

| | Notes |
|---|---|
| **Windows 10 1809+ or Windows 11** | Tested on Windows 11 26100 (x64) |
| **Python 3.10+** | Microsoft Store Python or [python.org](https://python.org) installer both work. `uv` is a fast alternative — see [macOS guide](./macos-setup.md#install-python-via-uv-recommended) |
| **USB-CDC driver** | Built into Windows 10/11. No `zadig` or `WinUSB` needed for ClawTouch firmware |

### Install Python via `uv` (recommended)

`uv` is a one-binary Python installer:

```powershell
# PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Set up an isolated env with Python 3.12
uv venv --python 3.12 C:\Users\<you>\clawtouch-test-env
```

Then install `clawtouch-mcp` (use the `[screenshot]` extra if you plan
to call `hid.screenshot`, e.g. with Claude Computer Use):

```powershell
C:\Users\<you>\clawtouch-test-env\Scripts\python.exe -m pip install "clawtouch-mcp[screenshot]"
```

## First-time Pico hookup

### No setup dialog — but watch for BOOTSEL mode

Unlike macOS (which pops a **Keyboard Setup Assistant** the first time you
plug in), Windows shows **no dialog** — the board just appears in Device
Manager as two `USB 串行设备 (COMx)` / `USB Serial Device (COMx)` entries.

There is, however, a Windows-visible failure mode worth recognising. If you
see a **removable drive labelled `RP2350` (~134 MB)** and **no COM ports**,
the board is sitting in the **RP2350 ROM bootloader (BOOTSEL)** — it
enumerates as `VID_2E8A&PID_000F` (RP2350 Boot + Mass Storage), *not* the
firmware's `PID_000B`. In this state `auto_detect_port()` returns `None` and
`list_pico_ports()` returns `[]`: the board is **invisible to detection**
because the bootloader exposes no CDC serial port.

```powershell
PS> [System.IO.Ports.SerialPort]::GetPortNames()     # empty!
PS> Get-Volume | Where-Object FileSystemLabel -eq 'RP2350'   # a ~134MB removable drive
```

**Fix: unplug and replug the Pico _without_ holding the white BOOTSEL
button.** If firmware is flashed it boots normally — the `RP2350` drive is
replaced by a ~3 MB `CIRCUITPY` drive and `COM5`/`COM6` appear
(`MI_00` = console, `MI_02` = data). Only a board with *no* firmware in
flash stays in BOOTSEL across a clean replug; that one needs reflashing.

### Pico shows up as TWO COM ports (this is normal)

The standard ClawTouch firmware enables a USB composite device with
two CDC channels:

```powershell
PS> [System.IO.Ports.SerialPort]::GetPortNames()
COM5
COM6
```

```powershell
PS> Get-PnpDevice -Class Ports -PresentOnly | Format-Table FriendlyName, InstanceId
FriendlyName              InstanceId
------------              ----------
USB Serial Device (COM5)  USB\VID_2E8A&PID_000B&MI_00\...
USB Serial Device (COM6)  USB\VID_2E8A&PID_000B&MI_02\...
```

- **VID `2E8A` / PID `000B`** = Raspberry Pi Pico 2 (RP2350)
- `MI_00` = REPL console (firmware debug output)
- `MI_02` = protocol data channel (HID commands)

`auto_detect_port()` automatically picks the data channel (the
higher-numbered COM, e.g. **COM6** in the example above). This was
fixed in **v0.2.1** after the previous version returned the REPL
console silently. If you're on v0.2.0 and see `ping()` return `False`
without an error, upgrade.

The trailing port number depends on USB enumeration order and changes
when you re-plug — don't hard-code it. Prefer `auto_detect_port()`,
fall back to `--port COM6` only when you want to pin a specific device.

### Natural number sort prevents the `COM10 < COM3` trap

Pre-v0.2.1 sorting used lexicographic order, which made `COM10` sort
before `COM3` on Windows and selected the wrong port when you had more
than 9 enumerated COM ports. v0.2.1+ uses natural numeric sort on the
trailing digits, so `COM3 < COM10 < COM200`. No action needed.

## Running tests

### Mock mode (no hardware)

```powershell
clawtouch-mcp --mock --log-level INFO
```

Should print:

```
... clawtouch_mcp.server: starting in MOCK mode — hardware is not touched
```

and accept JSON-RPC on stdin. Ctrl+C to exit. If the process appears
to hang and you can't get input echoed back, you may be on v0.2.0 or
v0.2.1 — these versions are **broken on Windows** because of an asyncio
stdin transport bug fixed in **v0.2.2**. Upgrade.

### Real Pico end-to-end check

```python
import asyncio
from clawtouch_mcp.bridge import SerialHidBridge, auto_detect_port

async def main():
    port = auto_detect_port()
    print(f"detected port: {port}")
    bridge = SerialHidBridge(port, timeout=2.0)
    await bridge.connect()
    assert await bridge.ping(), "ping failed — check firmware / port"
    print("ping OK")
    await bridge.mouse_move(100, 0, relative=True)
    print("moved cursor 100px right — check if your mouse jumped")
    await bridge.close()

asyncio.run(main())
```

Save to `e2e.py` and run with your venv's Python. Mouse cursor should
jump 100 pixels to the right (instantly — HID protocol sends one move
report, OS renders immediately; smoothing is not in scope for this OSS
layer).

### Full keyboard round-trip (type → select-all → copy → verify)

This is the Windows analogue of the macOS TextEdit/`pbpaste` check —
it confirms the whole HID keyboard path end to end. Open a **fresh,
empty Notepad tab first** and make sure it has focus (Notepad on Windows 11
is single-instance with tabs — the keystrokes land in the *active* tab, so
don't run this with an important document focused). The input method must be
**English** (see the IME section below) or the result will be mangled.

```python
import asyncio, ctypes
from clawtouch_mcp.bridge import SerialHidBridge, auto_detect_port

def get_clipboard():           # CF_UNICODETEXT via Win32, no extra deps
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetClipboardData.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    u32.OpenClipboard(0)
    try:
        h = u32.GetClipboardData(13)
        return ctypes.c_wchar_p(k32.GlobalLock(h)).value if h else ""
    finally:
        u32.CloseClipboard()

async def main():
    b = SerialHidBridge(auto_detect_port(), timeout=2.0)
    await b.connect()
    await b.key_combo(["ctrl"], "a")                       # select existing
    await b.type_text("hello world from clawtouch hid")    # types into Notepad
    await asyncio.sleep(0.3)
    await b.key_combo(["ctrl"], "a"); await b.key_combo(["ctrl"], "c")
    await asyncio.sleep(0.3)
    print(repr(get_clipboard()))                           # expect the exact string
    await b.release_all(); await b.close()

asyncio.run(main())
```

Expected: `'hello world from clawtouch hid'` — **byte-for-byte, lower-case
`h`**. Note the contrast with macOS: Windows Notepad has **no autocorrect**,
so the first letter is *not* capitalised the way macOS TextEdit silently
turns `hello` into `Hello`. (Verified on Windows 11 26100 with the data
channel on `COM6`.)

## Integrating with Claude Code (VS Code extension)

The Claude Code VS Code extension **does not** read `~/.claude.json`
top-level `mcpServers` even though the CLI does (verified on extension
version 2.1.143 in late 2026 — please re-verify against the current
extension version before assuming the gotcha still applies). Use a
project-scoped `.mcp.json` instead.

### Step 1 — write `.mcp.json` in your project root

```jsonc
// e:\YourProject\.mcp.json
{
  "mcpServers": {
    "clawtouch": {
      "type": "stdio",
      "command": "C:\\Users\\<you>\\clawtouch-test-env\\Scripts\\clawtouch-mcp.exe",
      "args": ["--allow-screenshot"]
    }
  }
}
```

**Don't pass `--screen WxH` unless you have a specific reason** — v0.2.3+
auto-detects the primary monitor's physical pixel size on startup
(`SetProcessDpiAwareness(2)` + `GetSystemMetrics(SM_CXSCREEN)` for the
primary monitor).
Mismatched `--screen` clamps clicks to the wrong rectangle: a 5120×1440
super-wide screen with `--screen 1920x1080` will silently fail to click
anything past x=1920 or y=1080.

Add `.mcp.json` to `.gitignore` — the `command` is a per-machine
absolute path that won't match other contributors' venvs.

### Step 2 — restart VS Code (full window close, not just the chat panel)

The Claude Code VS Code extension loads MCP servers at session
startup. Closing and re-opening the Claude chat panel is **not**
sufficient — you must close the whole VS Code window and reopen it.
After reopening, the extension may prompt: *"Trust this project's MCP
servers?"* — click **Trust / Approve**.

### Step 3 — verify with `/mcp`

In a new Claude chat, type `/mcp` (slash command). You should see:

```
clawtouch    connected   17 tools
```

(16 tools if you didn't pass `--allow-screenshot`; `hid.screenshot` is
opt-in by default.)

### Step 4 — confirm hardware

Ask Claude to call `device.info`. Expected output:

```json
{
  "info": {"port": "COM6", "connected": true, "seq": 0, ...},
  "screen": {"width": 5120, "height": 1440, "source": "detected"},
  "mcp_version": "0.4.6"
}
```

- `info.connected: true` = MCP server holds a live CDC connection
- `screen.source: "detected"` = auto-detect picked up your true monitor
  resolution. If it's `"unset"`, `_detect_screen()` failed — pass
  `--screen` explicitly to enable click clamping
- `info.port` should be your **higher-numbered** Pico COM port

## Known Windows-specific behaviors

### `alt+f4` from the agent closes the agent

If the MCP server runs on the **same PC** as the agent driving it and the
agent app (Claude Code / Cursor) is frontmost, a `hid.key("alt+f4")` lands
in the agent and closes it mid-task — real USB HID has no app targeting.
`hid.click` the target window first, or drive a remote target. Full table +
mitigations:
[INTEGRATIONS.md → "Known footgun: self-interrupt"](../examples/integrations/INTEGRATIONS.md#known-footgun-self-interrupt-on-a-shared-machine).

### Input method matters for `hid.type` — paste, don't type

`hid.type` sends raw US-ANSI HID keycodes; the active input method decides
what characters they become. With **Microsoft Pinyin** in **Chinese mode**,
those keycodes feed the IME composition buffer and the result is *not* what
you typed. Verified empirically on Windows 11 26100 (system Microsoft Pinyin,
typing into Notepad):

| `hid.type(...)` input | What lands in the document (Pinyin / 中文 mode) |
|---|---|
| `hello world from clawtouch hid` | `helloworldfromclaw透彻` — **every space is eaten** (space = "commit candidate") and `touch hi` is reinterpreted as the Chinese word **透彻** |
| `a:b;c,d.e!f?g` | `` (empty) — a letters+punctuation run is consumed by the composer and nothing commits |
| `nihao` + `hid.key("enter")` | `nihao` — Enter commits the raw pinyin letters (no valid candidate picked) |

The Windows langid stays `0x0804` whether Pinyin is in 中文 or 英文 sub-mode
(the 中/英 toggle is internal to the IME, not a separate keyboard layout), so
you **cannot** reliably detect the mode from `GetKeyboardLayout`. The same
input typed with the IME switched to **English** (tap `Shift`, or `Win+Space`)
comes through clean:

| `hid.type(...)` input | English mode |
|---|---|
| `hello world from clawtouch hid` | `hello world from clawtouch hid` — exact, spaces preserved |
| `a:b;c,d.e!f?g` | `a:b;c,d.e!f?g` — **ASCII colon/comma stay ASCII** (no fullwidth `：，`) |

**The robust fix for real text — paste via the clipboard.** This is what the
ClawTouch desktop product does and the pattern we recommend: never type
non-ASCII char-by-char; put the text on the clipboard and paste it, which
bypasses the IME composition buffer entirely. Verified to deliver the exact
string — Chinese, fullwidth punctuation, em-dash and emoji — **regardless of
IME mode**:

```powershell
Set-Clipboard '你好，ClawTouch 上线了！— emoji 🐾'   # or:  '...' | clip
```

then send the paste shortcut over HID: `bridge.key_combo(["ctrl"], "v")`.

Caveats: paste **overwrites the host clipboard** (save & restore it if the
user might be mid-copy — same shared-resource care as the self-interrupt
footgun above), and the target field must accept a paste. `hid.key`
shortcuts (`ctrl+c`, `alt+tab`, …) send named keycodes that bypass the IME
composition buffer and are unaffected by the input mode.

### Verified modifier-key combos

`hid.key` accepts `ctrl`, `alt`, `shift`, and `win` (aliases `gui` / `cmd`
all map to the same HID GUI modifier — on Windows that is the **Windows
key**, *not* a clipboard modifier). The clipboard/editing shortcuts live on
**`ctrl`**, not `cmd`. Combos exercised end-to-end against Notepad on
Windows 11 26100:

| Combo | Behavior |
|-------|----------|
| `ctrl+a` | Select all ✅ |
| `ctrl+c` / `ctrl+v` / `ctrl+x` | Copy / paste / cut ✅ (paste round-trips Chinese exactly) |
| `ctrl+z` | Undo ✅ |
| `win` / `gui` / `cmd` | Opens the Start menu (Windows key) — **not** ⌘; don't use it for clipboard |
| `alt+f4` | Closes the focused window — see the self-interrupt footgun above; **not** tested destructively |

### `hid.screenshot` needs no special permission (unlike macOS)

On macOS 14+ `mss` returns a **black image** until you grant Screen
Recording permission. **Windows has no such gate** — `hid.screenshot`
(and bare `mss`) capture the real desktop on first call, no prompt, no
settings toggle. Verified on Windows 11 26100: a full-screen grab of the
primary 5120×1440 monitor returned a 29.5 MB BGRA buffer with non-black
content immediately. The capture is the **physical** pixel grid and matches
`device.info.screen` (see the DPI note below), so no permission flow and no
scaling conversion are needed in your agent loop.

### Non-UTF-8 console code page (cp936) and the stdio transport

JSON-RPC over stdio is UTF-8 by spec, but on a **Chinese Windows install**
a *piped* `sys.stdout` defaults to the locale code page (`cp936` / GBK,
`sys.stdout.encoding == 'gbk'`). Builds **before** the
`tests/test_stdio_utf8_encoding.py` fix wrote the newline-delimited stdio
branch through that locale encoder, so any non-ASCII byte in a frame — a
single em-dash in a tool description is enough — was emitted as GBK and a
UTF-8 MCP client failed to decode it:

```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa1 in position ...
```

…and the session never established. The framed (Content-Length) transport
the VS Code extension uses was unaffected; the symptom only bit
newline-delimited clients on a GBK console. Current builds write UTF-8 bytes
on both transports. If you must run an older build, launch with
`PYTHONUTF8=1` (or `PYTHONIOENCODING=utf-8`) to force a UTF-8 stdout.

### Display scaling (DPI) does not affect HID coordinates

The Pico sends raw HID reports with physical-pixel coordinates. Windows
maps these directly to screen pixels regardless of the active scaling
factor (100%, 125%, 150%, 200% ...). `hid.screenshot` (via `mss`) also
returns the **physical** pixel grid. So:

- `hid.screenshot` resolution always matches `device.info.screen` —
  no scaling conversion needed in your agent loop
- `hid.click(x, y)` uses physical pixels — match coordinates you see in
  the screenshot

What scaling *does* affect: Windows apps that read `GetSystemMetrics`
without DPI awareness see scaled (logical) coordinates. v0.2.3+ marks
the server DPI-aware before measuring, so detection always returns
physical pixels.

### Multi-monitor setup

`_detect_screen()` reports the **primary monitor only** (`SM_CXSCREEN`
/ `SM_CYSCREEN`), not the virtual screen bounding box across all
monitors. The reason: `hid.screenshot` defaults to capturing the
primary monitor via `mss.monitors[1]`, so the clamp rectangle and the
screenshot share the same coordinate space — your agent can look at a
screenshot, pick a pixel, and click it without coordinate-system
translation.

If you want clicks to reach a secondary monitor, pass `--screen WxH`
explicitly with the bounding-box dimensions (e.g. `--screen 7680x1440`
for two 4K monitors side-by-side at 1440p) and use `hid.screenshot
--region` to capture the specific monitor you care about. The HID
device itself moves the OS cursor freely across all attached
monitors regardless of the clamp rectangle.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Tools don't appear in Claude chat after restart | `.mcp.json` not in project root, or you only closed the chat panel | Verify file exists at `<project>/.mcp.json`; close entire VS Code window |
| `clawtouch    failed` in `/mcp` panel | Server crashes on launch — usually wrong path or missing pyserial | Run the `command` from `.mcp.json` in PowerShell manually to see the error |
| Server appears to hang on Windows (v0.2.0/v0.2.1) | Asyncio `connect_read_pipe(sys.stdin)` is broken on Windows ProactorEventLoop | Upgrade to v0.2.2+ |
| `auto_detect_port()` returns `None` | Pico not plugged in, or wrong VID | Run `[System.IO.Ports.SerialPort]::GetPortNames()` — should show two COMs |
| `ping()` returns `False` on v0.2.0 | Old dual-CDC detection bug | Upgrade to v0.2.1+ |
| `hid.click` does nothing past x=1920 or y=1080 | `--screen 1920x1080` clamping a larger screen | Remove `--screen` from args (let v0.2.3+ auto-detect), or pass your real resolution |
| `device.info` returns `screen.source: "unset"` | Auto-detect failed on this machine | Pass `--screen WxH` explicitly. Open an issue with your Windows version |
| `hid.type` produces wrong characters / Chinese / dropped spaces | An IME (Microsoft Pinyin) is in 中文 mode | Switch to "ENG" (`Win+Space`, or tap `Shift` in Pinyin), or paste instead of type — see the input-method section |
| `auto_detect_port()` returns `None` **and** a `RP2350` drive is mounted | Board is in BOOTSEL (`PID_000F`), no CDC port | Replug without holding BOOTSEL — see "watch for BOOTSEL mode" |
| Client fails with `UnicodeDecodeError: ... byte 0xa1` on connect | Old build emitting GBK on a cp936 console | Update; or launch with `PYTHONUTF8=1` — see the cp936 note |
| `Permission denied: 'COM6'` | Another process holds the port | Check Device Manager — close any PuTTY/serial-terminal session, or unplug/replug the Pico |

## Compatibility matrix

| | Verified on | Notes |
|---|---|---|
| Windows 11 (x64) | 26100, Python 3.13.12 (uv), Pico 2 RP2350 | ✅ Full e2e: dual-CDC detect → `COM6` → `ping` → `type`/`ctrl+a`/`ctrl+c` clipboard round-trip → Chinese paste → `hid.screenshot`; MCP server `device.info` reports `screen 5120×1440 source=detected`. IME-mangling vs English vs paste all characterised on a cp936 console |
| Windows 10 1809+ | _not yet tested_ | Same code paths — should work |
| Windows 10 pre-1809 | unsupported | `SetProcessDpiAwareness(2)` (per-monitor v2) needs 1809+. v1 fallback works on older |
| Pico 2 (RP2350) | Yes | VID `0x2E8A`, PID `0x000B` enumerated correctly |
| Claude Code VS Code extension 2.1.x | 2.1.143 verified | Earlier versions may have different MCP loading semantics |
| Claude Desktop (Windows) | _not yet tested_ | Reads config from `%APPDATA%\Claude\claude_desktop_config.json` — same `mcpServers` shape |
