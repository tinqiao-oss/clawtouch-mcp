# Computer Use × ClawTouch HID

Two reference demos showing how to wire LLM Computer Use loops to a
physical ClawTouch HID device. Both run on a **single machine** (the
LLM agent and the target are the same PC) — the value is replacing
software-synthesized mouse/keyboard with real USB HID input.

| Demo | Provider | File | API |
|------|----------|------|-----|
| Claude Computer Use | Anthropic | [claude_demo.py](claude_demo.py) | `client.beta.messages.create` with `computer_20250124` tool |
| OpenAI CUA | OpenAI | [openai_cua_demo.py](openai_cua_demo.py) | Responses API with `computer-use-preview` model |

## Why bother?

Both providers' Computer Use loops normally use software input
synthesis (`xdotool`, `pyautogui`, browser DOM events). These demos
replace **only the action execution path** with the ClawTouch USB
device. Perception (screenshots) and reasoning (the LLM) are
unchanged.

What this gets you:

- Input events traverse the OS HID driver stack at the USB layer —
  identical to plugging in a real keyboard/mouse — useful for
  compatibility testing, accessibility scenarios, or environments
  where synthetic input is blocked.
- Same agent prompts, same reasoning model, same loop shape as the
  vanilla demos — just a different action sink.

What it does **not** get you:

- These demos run on **one machine**. To control a different machine
  from the agent's machine, you also need a screenshot path back from
  the target (HDMI capture card or a thin agent on the target).
- ClawTouch's safety rails (`--screen` clamping, `--ops-per-sec`
  rate limit) only apply when you go through `clawtouch-mcp`. When
  importing the bridge directly as these demos do, **your application
  is responsible for bounds checking and pacing.**

## Architecture

```
┌────────────────────────┐
│ Claude / OpenAI CUA    │  ← LLM picks the next action
│ (reasoning + planning) │
└──────────┬─────────────┘
           │ tool_use(action="left_click", coordinate=[500,300])
           ▼
┌────────────────────────┐
│ This demo script       │  ← maps action → HID primitive
│ (orchestrator loop)    │
└──────────┬─────────────┘
           │ bridge.mouse_move(500,300) + mouse_click("left")
           ▼
┌────────────────────────┐
│ SerialHidBridge        │  ← clawtouch_mcp.bridge
│ (USB CDC serial)       │
└──────────┬─────────────┘
           │ framed protocol bytes
           ▼
┌────────────────────────┐
│ Pico 2 + firmware      │  ← emits real USB HID reports
└──────────┬─────────────┘
           │ USB HID
           ▼
┌────────────────────────┐
│ OS (Win / macOS / Linux)│  ← sees a real keyboard + mouse
└────────────────────────┘
```

## Install

```bash
pip install clawtouch-mcp[screenshot]  # mss for screenshots
pip install anthropic                  # for claude_demo.py
pip install openai                     # for openai_cua_demo.py
```

Set the API key for whichever provider you're using:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

## Run

```bash
# Real Pico, real screen
python claude_demo.py "open my browser and search for ClawTouch"

# No hardware — actions are logged but nothing moves
python claude_demo.py --mock "open my browser and search for ClawTouch"

# Specify port + screen dimensions
python claude_demo.py --port COM7 --screen 1920x1080 "..."
```

## What's mapped

Both demos map the standard Computer Use action vocabulary to
ClawTouch HID primitives:

| LLM action | HID call |
|------------|----------|
| `screenshot` | `mss.grab()` → PNG → base64 (does NOT go through HID) |
| `mouse_move(x, y)` | `bridge.mouse_move(x, y)` |
| `left_click` / `right_click` / `middle_click` | `bridge.mouse_click(button)` |
| `double_click` | `bridge.mouse_click("left", double=True)` |
| `type(text)` | `bridge.type_text(text)` |
| `key(text)` | `bridge.key_combo(modifiers, key)` (parses "ctrl+c") |
| `scroll(direction, amount)` | `bridge.mouse_scroll(±delta)` |
| `cursor_position` | Not supported (HID is fire-and-forget); returns `(0, 0)` |
| `left_click_drag` | Limited (current bridge has no separate button-down) — best-effort move + click |

## Regulatory note for deployments touching end users

If you deploy a Computer Use agent that posts AI-generated content
into chat apps, emails, comments, or other channels reaching end
users, several jurisdictions now require **conspicuous labeling** of
that content:

- **China** — *Provisions on the Administration of Deep Synthesis
  Internet Information Services* (互联网信息服务深度合成管理规定,
  effective 2023-01-10), §16 / §17.
- **EU** — *AI Act* Article 50 (deployers of generative AI systems
  must disclose AI-generated text / image / audio / video).

Typical implementations: prepend `[AI]` (or a localized equivalent)
to outgoing text, embed a visible watermark in generated media, or
expose a UI toggle that users can verify. These demos do **not** add
such labels — your application is responsible for adding them
before any AI output reaches a third-party platform or end user.

## Safety notes

Computer Use loops will drive your real mouse and keyboard with no
human in between. Best practices:

- **Set explicit screen bounds.** Pass `--screen WxH` matching your
  actual display; the demo clamps coordinates so the agent can't move
  the cursor to bogus pixel positions.
- **Keep `--ops-per-sec` modest while iterating.** Default is 10 in
  these demos; raise after you trust the agent.
- **Have a panic stop ready.** Unplugging the Pico's USB cable is
  instant and safe.
- **Don't leave it unattended.** The vanilla demo loops will happily
  burn API tokens for hours if a task drifts.

## Comparison with `clawtouch-mcp` server mode

These demos talk to the HID device **directly** via the Python
`SerialHidBridge` — they don't go through the `clawtouch-mcp` server
subprocess. The MCP server is for the inverse pattern (an MCP client
like Claude Desktop drives the HID device via stdio JSON-RPC).

| | This demo | `clawtouch-mcp` |
|---|---|---|
| Who drives | Your Python script | An MCP client (Claude Desktop, Cursor, etc.) |
| LLM call | You call `client.messages.create` | The client calls the API |
| HID dispatch | Direct `bridge.mouse_*` calls | JSON-RPC `tools/call` → server → bridge |
| Safety rails | Your code | Server enforces (`--screen`, `--ops-per-sec`) |
| Use when... | You're building a Computer Use loop | You're plugging HID into an existing agent |

## Looking for a packaged Computer Use loop?

These demos give you the **action sink** (real HID instead of synthetic
input). The reasoning loop, screenshot pipeline, UI element detection,
and application-specific adapters are still your job.

The ClawTouch desktop product packages that whole loop end-to-end:
vision-based UI element detection, multi-step task orchestration, and
built-in adapters for WeChat / browser / common desktop apps — no glue
code required. That's the closed-source side of the open-core split.
See [clawtouch.cn](https://clawtouch.cn) or write `support@tinqiao.com`
for evaluation access.
