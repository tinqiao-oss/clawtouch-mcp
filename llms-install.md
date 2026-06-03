<!-- mcp-name: io.github.tinqiao-oss/clawtouch-mcp -->

# Installing clawtouch-mcp (Cline / AI-assisted setup)

This file gives an AI assistant (e.g. Cline) the exact steps to install and
configure **clawtouch-mcp**, an MCP stdio server that exposes physical USB HID
mouse / keyboard actions as agent tools. See [README.md](README.md) for the
full reference; this is the condensed install path.

## Prerequisites

- **Python 3.10+** with `pip` available.
- **Hardware (for real input):** a ClawTouch HID device, or any Raspberry Pi
  Pico 2 / RP2350 board flashed with the open
  [clawtouch-hid](https://github.com/tinqiao-oss/clawtouch-hid) firmware,
  connected over USB.
- **No hardware yet?** Run in `--mock` mode: every tool is registered and
  callable, calls are logged, nothing physically moves. Use this to verify the
  install end-to-end before any hardware is attached.
- No API key, no account, no cloud, no network access — the server speaks only
  stdio JSON-RPC to the client and USB serial to the board.

## Step 1 — Install

```bash
pip install clawtouch-mcp
```

Optional screenshot tool (`hid.screenshot`, off by default) needs extra deps:

```bash
pip install "clawtouch-mcp[screenshot]"
```

## Step 2 — Register the server with Cline

Add this entry to Cline's `cline_mcp_settings.json` (the `mcpServers` map).

**First-run verification with no hardware (recommended — confirms setup works
even before a device is attached):**

```json
{
  "mcpServers": {
    "clawtouch": {
      "command": "clawtouch-mcp",
      "args": ["--mock", "--log-level", "INFO"]
    }
  }
}
```

**With real hardware**, drop `--mock` and let it auto-detect the board + screen:

```json
{
  "mcpServers": {
    "clawtouch": {
      "command": "clawtouch-mcp",
      "args": []
    }
  }
}
```

On Windows, pass the COM port explicitly if auto-detect picks the wrong one, and
optionally clamp coordinates to one monitor:

```json
{
  "mcpServers": {
    "clawtouch": {
      "command": "clawtouch-mcp",
      "args": ["--port", "COM7", "--screen", "1920x1080"]
    }
  }
}
```

> If the `clawtouch-mcp` console script is not on PATH in your environment, use
> `"command": "python"` with `"args": ["-m", "clawtouch_mcp", "--mock", "--log-level", "INFO"]`
> instead — identical flags.

## Step 3 — Verify

After Cline reloads its MCP servers, the `clawtouch` server should connect and
expose **15 tools**: 13 always-on `hid.*` input tools + 2 read-only `device.*`
tools (`hid.screenshot` adds a 16th only when you pass `--allow-screenshot`).
On startup the server logs `13 HID tools + 2 device tools registered` to
stderr. Call `device.info` to see the active connection — in `--mock` it reports
the mock backend.

## Safety

This grants an agent real keyboard / mouse reach over the host — the same reach
as a person at the keyboard. Read the **Safety** section of
[README.md](README.md#safety) before connecting an autonomous agent: prefer a
dedicated / wipeable machine and a least-privilege account, keep a human in the
loop for consequential actions, and keep `hid.release_all` (or simply unplugging
the device) reachable as a panic stop.
