**English** | [简体中文](README.zh-CN.md)

# clawtouch-mcp

> **Give your LLM agent real hands.**
> An MCP server that turns any MCP-compatible client — [Claude Desktop](https://claude.ai/download),
> [Cline](https://github.com/cline/cline), [Continue](https://github.com/continuedev/continue),
> [Cursor](https://www.cursor.com/), [OpenClaw](https://github.com/openclaw),
> [Hermes Agent](https://github.com/NousResearch/hermes-agent) and any other —
> into something that can move a real mouse and press real keys through a USB HID device.

🌐 **[clawtouch.cn](https://clawtouch.cn)** — official site for hardware, docs, and commercial inquiries.

[![PyPI version](https://img.shields.io/pypi/v/clawtouch-mcp.svg)](https://pypi.org/project/clawtouch-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/clawtouch-mcp.svg)](https://pypi.org/project/clawtouch-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Commercial: clawtouch.cn](https://img.shields.io/badge/commercial-clawtouch.cn-orange.svg)](https://clawtouch.cn)

---

## What is this?

A standalone Python process that speaks **Model Context Protocol** (MCP) over
stdio and exposes mouse / keyboard primitives to whatever LLM agent you have.
Under the hood it talks to a **ClawTouch HID device** — a Raspberry Pi Pico 2
running the open [ClawTouch HID firmware](#hardware) over USB serial — and
translates `hid.click` / `hid.type` / `hid.scroll` tool calls into real HID
reports that travel through the OS HID driver stack on **the same input path
as any plugged-in external keyboard or mouse**.

**Why care?** Most "AI controls your computer" demos require an agent
process running on the target machine and route through OS-level synthetic
event APIs. Those approaches don't fit locked-down kiosks, embedded test
harnesses, or cross-machine RPA where the target must stay clean. A
physical USB HID peripheral routes input through the standard OS HID
driver stack — the same path as any plug-in keyboard or mouse — and
needs zero software installed on the target.

> 📦 MIT-licensed. No ClawTouch backend, no LLM, no input-pacing layer. Just
> the raw HID plumbing so other agent stacks can talk to real hardware.

## Scope — what this is and isn't

**One device, one target.** The hardware is a USB peripheral with a
single host connection. Whatever you plug it into is the one machine
it can drive. That's by design — this is a tethered control device,
not a fleet automation tool. If you want to drive ten machines you buy
ten devices.

We support these scenarios:

- **RPA / test automation** — bridge an AI agent to an old machine you
  can't install software on, a kiosk shell, an industrial PC running
  an unsupported OS, or a phone in your QA lab.
- **Accessibility** — let a disabled user drive their own computer via
  a screen reader plus an LLM agent issuing HID commands, instead of
  fighting with synthetic-input compatibility on each app.
- **Compatibility testing** — verify your software treats external HID
  input correctly, which can differ from injected synthetic input.
- **Cross-machine workflows** — an agent on your dev laptop driving
  the test machine in the rack, with no agent install on the target.

We do **not** support, document, or assist with:

- **Mass account creation / multi-account operations** on consumer
  platforms (WeChat, Douyin, Instagram, etc.) — a single-host tethered
  peripheral is structurally a poor fit, and the use case is regulatory
  red-line territory in most jurisdictions.
- **Application-specific scripted shortcuts** (selectors, fixed-flow
  scripts for a particular site or app). Those belong in agent / RPA
  frameworks built on top of this primitive layer.

If you're looking for either of the above, this isn't the right tool.

## Install

```bash
pip install clawtouch-mcp                 # minimal (serial only)
pip install 'clawtouch-mcp[screenshot]'   # + mss for hid.screenshot tool
```

**Platform-specific setup guides** (recommended on first install):

* **Windows** — [`docs/windows-setup.md`](docs/windows-setup.md): dual
  COM port enumeration, VS Code Claude extension `.mcp.json` config,
  full window restart required, display-scaling notes.
* **macOS** — [`docs/macos-setup.md`](docs/macos-setup.md): Keyboard
  Setup Assistant dialog on first plug-in, dual USB-CDC ports, Screen
  Recording permission, Pinyin IME punctuation gotchas.

## Run

```bash
# 1. Auto-detect HID board AND auto-detect screen size (v0.2.3+)
clawtouch-mcp

# 2. Explicit port (Windows), screen still auto-detected
clawtouch-mcp --port COM7

# 3. Pin screen size manually (e.g. clamp to one monitor in a multi-monitor setup)
clawtouch-mcp --screen 1920x1080

# 4. No hardware — everything is logged, nothing moves (dev/CI mode)
clawtouch-mcp --mock --log-level INFO
```

> v0.2.3+ auto-detects the primary monitor's physical pixel size on
> startup so coordinates clamp to the actual screen rather than a
> hard-coded `1920x1080`. Use `device.info` from your MCP client to
> see what was detected (`screen.source` is `"detected"` /
> `"explicit"` / `"unset"`).

## Use with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

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

Restart Claude Desktop. You should see `clawtouch` show up in the MCP server
list with 9 tools available. Try:

> Take a screenshot of my screen, find the search box, click it, and type
> "hello world".

(Requires `--allow-screenshot` to enable the `hid.screenshot` tool — off by
default for privacy.)

## Use with other MCP clients

Copy-pasteable config snippets for 7 verified clients live in
[`examples/integrations/`](examples/integrations/):

- [Claude Desktop / Claude Code](examples/integrations/claude_desktop.md)
- [Cursor](examples/integrations/cursor.md)
- [OpenClaw](examples/integrations/openclaw.md)
- [Hermes Agent (NousResearch)](examples/integrations/hermes_agent.md)
- [ChatGPT Desktop + OpenAI Codex CLI](examples/integrations/openai.md)
- [Cherry Studio 🇨🇳](examples/integrations/cherry_studio.md)
- [Trae IDE (ByteDance) 🇨🇳](examples/integrations/trae_ide.md)

Each file has the verify-it-works steps and common gotchas for that
client. PRs adding new clients welcome.

## Use with Computer Use loops

If you're building your own Computer Use loop (instead of plugging
into an MCP client), see [`examples/computer_use/`](examples/computer_use/)
for two reference implementations that route Anthropic / OpenAI agent
actions through ClawTouch HID:

- [Claude Computer Use → HID](examples/computer_use/claude_demo.py) —
  `client.beta.messages.create` with the `computer_20250124` tool
- [OpenAI CUA → HID](examples/computer_use/openai_cua_demo.py) —
  Responses API with `computer-use-preview`

Both demos import `clawtouch_mcp.bridge.SerialHidBridge` directly (no
MCP subprocess) and run on a single machine.

## Application skills (LLM guidance)

[`clawtouch-skills`](https://github.com/tinqiao-oss/clawtouch-skills)
is a companion repository of **markdown skill files** — operator
manuals for specific applications that an LLM can load before driving
that app through `clawtouch-mcp`. The first batch covers Chinese-
market apps where LLM training data is thin and the delta between
"LLM guesses" and "actual UI" is widest:

- WPS Office, Feishu / Lark, DingTalk —
  see [`tinqiao-oss/clawtouch-skills`](https://github.com/tinqiao-oss/clawtouch-skills)

Skills are soft guidance (the LLM still decides), not deterministic
execution. For guaranteed deterministic flows with audit trail and
SLA, see [`docs/COMMERCIAL_PRODUCT.md`](docs/COMMERCIAL_PRODUCT.md).

## Tools exposed

| Tool              | Purpose                                       |
|-------------------|-----------------------------------------------|
| `hid.click`       | Click at absolute (x, y)                      |
| `hid.move`        | Move mouse (absolute or relative)             |
| `hid.hover`       | Move, then idle                               |
| `hid.type`        | Type a UTF-8 string                           |
| `hid.scroll`      | Wheel scroll (positive = down, negative = up) |
| `hid.key`         | Named key / shortcut (`enter`, `ctrl+c`, …)   |
| `hid.release_all` | Panic stop — release every held button / key  |
| `hid.screenshot`  | PNG screenshot of primary monitor (opt-in)    |
| `device.list`     | List candidate HID board ports                |
| `device.info`     | Active connection info                        |

## Safety

* Coordinates **clamped** to `--screen WxH` so an agent can't move the mouse
  to bogus pixel positions.
* Typed text **capped at 4096 chars** per call.
* All operations **rate-limited** to `--ops-per-sec` (default 20).
* `hid.screenshot` is **disabled unless** you pass `--allow-screenshot`.
* `hid.release_all` exposed for use as a panic-stop tool from the agent.

## Hardware

This server can talk to:

1. **ClawTouch HID device** — turnkey hardware, drop-shipped, plug-and-play.
   Order or get a sample at [clawtouch.cn](https://clawtouch.cn).
2. **Any RP2350 board running [clawtouch-hid](https://github.com/tinqiao-oss/clawtouch-hid)** —
   the OSS firmware + frozen v1.0 protocol live in their own public repo.
   Buy a Pico 2 (~$8), flash the firmware, you're done.

The wire protocol is the same for both — the server doesn't care which one it
talks to.

## FAQ

**Does this need a ClawTouch account / API key / cloud service?**
No. This server only speaks USB serial to the HID board. There's no network
call. No data leaves your machine.

**Can I use this without buying ClawTouch hardware?**
Yes — buy an $8 Raspberry Pi Pico 2, flash the open-source
[clawtouch-hid](https://github.com/tinqiao-oss/clawtouch-hid) firmware,
and the server will talk to it the same way as the turnkey device.

**Why HID and not just OS-level mouse / keyboard APIs (pyautogui etc.)?**
OS-level synthetic input requires an agent process on the target machine
and may behave differently from real input in locked-down kiosks, embedded
test harnesses, or accessibility-tech compatibility rigs. A USB HID
peripheral routes through the standard OS HID driver stack and works in
environments where no software can be installed on the target — kiosk
automation, offline test rigs, accessibility tooling, and cross-machine
RPA.

**Is there a JavaScript / TypeScript version?**
Not yet. `clawtouch-bridge-sdk` (Python + Node) is planned — see roadmap.

**How is this different from the closed-source ClawTouch desktop app?**
This MCP server is just the bottom layer — raw HID primitives — so
other agent stacks can use ClawTouch hardware without adopting the
whole ClawTouch product. → For what the desktop product adds on top
(vision, orchestration, app adapters, B2B layer), see
[`docs/COMMERCIAL_PRODUCT.md`](docs/COMMERCIAL_PRODUCT.md).

## Open source roadmap

ClawTouch follows an **open-core** model: hardware and protocol primitives
are open, the integrated commercial product stays closed.

| Component                              | Status                       |
|----------------------------------------|------------------------------|
| **clawtouch-mcp**                      | ✅ Released (this repo)      |
| **[clawtouch-hid](https://github.com/tinqiao-oss/clawtouch-hid)** (firmware + frozen v1.0 protocol) | ✅ Released |
| **[clawtouch-skills](https://github.com/tinqiao-oss/clawtouch-skills)** (markdown skill files for LLM agents) | ✅ Released |
| **clawtouch-bridge-sdk** (Python + Node HID SDK)   | 🔵 Future       |
| Backend / desktop app / adapters / vision models   | 🔒 Closed source — [what's in it](docs/COMMERCIAL_PRODUCT.md) |

The dates aren't fixed — we ship when each piece is properly polished. Star
the org [@tinqiao-oss](https://github.com/tinqiao-oss) to get notified.

## Architecture overview

```
┌─────────────────────┐       stdio JSON-RPC      ┌─────────────────────┐
│ Claude Desktop /    │ ◄──────────────────────► │  clawtouch-mcp      │
│ Cline / OpenClaw    │                          │  (this repo)        │
└─────────────────────┘                          └──────────┬──────────┘
                                                            │ USB serial (CDC)
                                                            ▼
                                                 ┌─────────────────────┐
                                                 │  Pico 2 + ClawTouch │
                                                 │  HID firmware       │
                                                 └──────────┬──────────┘
                                                            │ USB HID
                                                            ▼
                                                 ┌─────────────────────┐
                                                 │  Your operating     │
                                                 │  system (Win/Mac/   │
                                                 │  Linux)             │
                                                 └─────────────────────┘
```

See [`docs/TECHNICAL_WALKTHROUGH.md`](docs/TECHNICAL_WALKTHROUGH.md) for a
frame-by-frame trace of what happens when an agent calls `hid.click`.

For the bigger picture — how this MCP server fits into the larger
Perception → Decision → Action loop ClawTouch uses, where data goes, and
how the closed-source desktop app layers on top of the open HID
primitives below — see the official technical documentation:

* [System architecture &amp; data flow](https://clawtouch.cn/en/docs/architecture.html) — the three-layer model and how it compares to RPA / AutoHotkey / browser-extension automation
* [Data security &amp; compliance](https://clawtouch.cn/en/docs/security.html) — what stays local, what crosses the network, what's encrypted

## Contributing

PRs are welcome for: new MCP tools that map to existing HID primitives, bug
fixes, additional client integration examples, doc improvements,
non-English README translations.

We're _not_ taking PRs for: low-level input timing or behavioral-modeling
layers (intentionally out of scope — see [open source roadmap](#open-source-roadmap)),
adapters for specific applications (those live in the closed-source
desktop app).

## About

`clawtouch-mcp` is maintained by **Tinqiao Technology** — the team behind
**ClawTouch** ([clawtouch.cn](https://clawtouch.cn)), building plug-in USB
devices that let LLM agents operate real Windows / macOS / Linux desktops
at the HID layer. This MCP server is the open, primitive piece of that
stack — see the [open source roadmap](#open-source-roadmap) for what's
open vs. closed.

## License

MIT © Tinqiao Technology (Beijing) Co., Ltd. See [LICENSE](LICENSE).

For commercial deployments at scale, enterprise support, or OEM hardware
discussion: `support@tinqiao.com`.
