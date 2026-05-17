# Windows Setup Guide

Verified on Windows 11 (build 26100, x64) with a Raspberry Pi Pico 2
running ClawTouch HID firmware v1.0, driving the Claude Code VS Code
extension over MCP stdio. Most of the gotchas below apply to Windows
10 1809+ too; the few that don't are flagged inline.

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

## Integrating with Claude Code (VS Code extension)

The Claude Code VS Code extension **does not** read `~/.claude.json`
top-level `mcpServers` even though the CLI does (as of 2.1.143). Use
a project-scoped `.mcp.json` instead.

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
clawtouch    connected   10 tools
```

(9 tools if you didn't pass `--allow-screenshot`; `hid.screenshot` is
opt-in by default.)

### Step 4 — confirm hardware

Ask Claude to call `device.info`. Expected output:

```json
{
  "info": {"port": "COM6", "connected": true, "seq": 0, ...},
  "screen": {"width": 5120, "height": 1440, "source": "detected"},
  "mcp_version": "0.2.3"
}
```

- `info.connected: true` = MCP server holds a live CDC connection
- `screen.source: "detected"` = auto-detect picked up your true monitor
  resolution. If it's `"unset"`, `_detect_screen()` failed — pass
  `--screen` explicitly to enable click clamping
- `info.port` should be your **higher-numbered** Pico COM port

## Known Windows-specific behaviors

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
| `hid.type` produces wrong characters | Active keyboard layout isn't US-ANSI | Switch input method to "ENG" in the system tray (Win+Space cycles) |
| `Permission denied: 'COM6'` | Another process holds the port | Check Device Manager — close any PuTTY/serial-terminal session, or unplug/replug the Pico |

## Compatibility matrix

| | Verified on | Notes |
|---|---|---|
| Windows 11 (x64) | 26100, MSVC Python 3.13 | ✅ Full e2e through VS Code Claude extension |
| Windows 10 1809+ | _not yet tested_ | Same code paths — should work |
| Windows 10 pre-1809 | unsupported | `SetProcessDpiAwareness(2)` (per-monitor v2) needs 1809+. v1 fallback works on older |
| Pico 2 (RP2350) | Yes | VID `0x2E8A`, PID `0x000B` enumerated correctly |
| Pico W (RP2040) | _not yet tested_ | Shares VID `0x2E8A` so detection works as-is. May need single-CDC fallback if its firmware enumerates only one port — handled by `list_pico_ports()` already. |
| Claude Code VS Code extension 2.1.x | 2.1.143 verified | Earlier versions may have different MCP loading semantics |
| Claude Desktop (Windows) | _not yet tested_ | Reads config from `%APPDATA%\Claude\claude_desktop_config.json` — same `mcpServers` shape |
