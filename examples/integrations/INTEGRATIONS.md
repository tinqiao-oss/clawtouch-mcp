# MCP Client Integrations

Copy-pasteable configuration for the most common MCP clients. The
server itself is the same in every case (`clawtouch-mcp` over stdio
JSON-RPC) — only the config file location and JSON/YAML shape differs.

## Common shape

Every client config boils down to the same four facts:

1. **Command** to launch — `clawtouch-mcp` after `pip install clawtouch-mcp`.
2. **Args** — `--port COMx` (or auto-detect), `--screen WIDTHxHEIGHT`
   to clamp coordinates, `--allow-screenshot` to enable the screenshot
   tool (off by default for privacy).
3. **Transport** — stdio for everything below. The server does not
   currently expose HTTP/SSE; wrap with
   [supergateway](https://github.com/supercorp-ai/supergateway) if your
   client requires HTTP.
4. **Restart the client** after editing — most don't hot-reload.

After restarting, ask: *"List the MCP tools you have available."* You
should see `hid.click`, `hid.move`, `hid.hover`, `hid.type`,
`hid.scroll`, `hid.key`, `hid.release_all`, `device.list`, `device.info`
(and `hid.screenshot` if `--allow-screenshot`).

## Verified clients

- [Claude Desktop / Claude Code](#claude-desktop--claude-code) — Anthropic
- [Cursor](#cursor) — Cursor IDE
- [OpenClaw](#openclaw) — agent harness
- [Hermes Agent](#hermes-agent) — NousResearch
- [ChatGPT Desktop / Codex CLI](#chatgpt-desktop--codex-cli) — OpenAI
- [Cherry Studio](#cherry-studio) — Chinese MCP-native client
- [Trae IDE](#trae-ide) — ByteDance

PRs welcome for additional clients (Goose, Continue, Zed, ChatBox, n8n,
custom code). Open a PR adding a section once you've verified the
config works. For clients where MCP support is announced but not yet
shipped, please wait until the feature is in a stable release.

---

## Claude Desktop / Claude Code

[Claude Desktop](https://claude.ai/download) and
[Claude Code](https://docs.claude.com/claude-code) are Anthropic's
official clients and the reference implementation for MCP.

**Config file location:**

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

If the file doesn't exist, create it.

**Config:**

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

Adjust `--port` to your hardware (`COM7` Windows /
`/dev/cu.usbmodem...` macOS / `/dev/ttyACM0` Linux), or omit to
auto-detect via USB VID 0x2E8A.

For `Claude Code` CLI: same JSON shape under `~/.claude/mcp.json` (or
project-scoped `.mcp.json` at repo root); see the
[official docs](https://docs.claude.com/claude-code/mcp).

**Optional — enable screenshot tool:** add `--allow-screenshot` to
args. Requires `pip install 'clawtouch-mcp[screenshot]'`. On macOS 14+
grant the Claude Desktop process **Screen Recording permission** in
System Settings → Privacy & Security.

**Verify:** Restart Claude Desktop fully (Cmd+Q, then relaunch —
closing the window is not enough). Ask in a new chat: *"List the MCP
tools you have available."*

---

## Cursor

[Cursor](https://www.cursor.com/) is an AI-first VS Code fork with
built-in MCP support since v0.45+.

**Per-project (recommended):** create `.cursor/mcp.json` at repo root:

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

Commit this file so teammates get the same setup.

**Global:** Settings → MCP → Add new MCP server. Or paste JSON into the
global config (Settings → MCP shows the exact path).

**Verify:** Cursor's Settings → MCP page shows a green dot next to
`clawtouch` and lists 9 tools (10 with `--allow-screenshot`).

**Note:** Cursor's agent mode will happily call `hid.click` on whatever
coordinate it thinks is right — including outside the Cursor window.
Set `--screen WxH` to your actual display and keep `hid.release_all`
in mind as a panic stop.

---

## OpenClaw

[OpenClaw](https://github.com/openclaw) is a locally-running AI agent
harness that treats MCP servers as first-class tool providers alongside
its native skill packages.

OpenClaw stores MCP server configs in `~/.openclaw/mcp.json` (Linux /
macOS) or `%APPDATA%\openclaw\mcp.json` (Windows). Same JSON shape as
Claude Desktop's, or use the CLI:

```bash
openclaw mcp add clawtouch \
    --command clawtouch-mcp \
    --arg --port=COM7 \
    --arg --screen=1920x1080
```

Restart OpenClaw (or `openclaw mcp reload`) — `openclaw mcp list` shows
`clawtouch (connected, 9 tools)`.

Official MCP docs: <https://docs.openclaw.ai/cli/mcp>

---

## Hermes Agent

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is
NousResearch's agentic framework. Its MCP client connects over stdio
or HTTP/StreamableHTTP.

Hermes reads MCP servers from `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  clawtouch:
    transport: stdio
    command: clawtouch-mcp
    args:
      - --port
      - COM7
      - --screen
      - 1920x1080
```

Verify with `hermes-agent mcp probe clawtouch`.

Official MCP docs: <https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp>

---

## ChatGPT Desktop / Codex CLI

OpenAI ships MCP client support in **ChatGPT Desktop** (Developer
Mode), **OpenAI Codex CLI**, and the **Apps SDK / Agents SDK /
Responses API** (programmatic). Eligibility, plan requirements, and
beta gating change over time — the official docs are the source of
truth:

- ChatGPT Developer Mode — <https://platform.openai.com/docs/guides/developer-mode>
- Codex MCP — <https://developers.openai.com/codex/mcp>
- Apps SDK MCP — <https://developers.openai.com/apps-sdk/concepts/mcp-server>

**ChatGPT Desktop (Developer Mode):**

1. Enable Developer Mode — Settings → Beta features → Developer mode
   (check the help center for current eligibility).
2. Settings → Connectors → "Add MCP server" → Name: `clawtouch`,
   Type: `Local (stdio)`, Command: `clawtouch-mcp`, Args:
   `--port COM7 --screen 1920x1080`. Enable "write" if you want the
   agent to actually click and type.
3. Approve the server on first launch.

**OpenAI Codex CLI:** reads MCP from `~/.codex/config.toml`:

```toml
[mcp.servers.clawtouch]
command = "clawtouch-mcp"
args = ["--port", "COM7", "--screen", "1920x1080"]
```

**Disclaimer:** Developer Mode + a write-capable MCP server means the
agent can move your real mouse and type real keys. Set `--screen WxH`
to your actual display, keep `--ops-per-sec` low while iterating, and
have `hid.release_all` plus the Pico USB cable as panic stops.

---

## Cherry Studio

[Cherry Studio](https://github.com/CherryHQ/cherry-studio) is a
popular Chinese cross-platform AI desktop client (made by Shanghai
千汇科技 / Qianhui Tech). 50+ LLM providers, native MCP server
management UI.

UI: **设置 (Settings)** → **MCP 服务器 (MCP Servers)** → **添加 (Add)**.

| Field | Value |
|-------|-------|
| 名称 (Name) | `clawtouch` |
| 类型 (Type) | `stdio` |
| 命令 (Command) | `clawtouch-mcp` |
| 参数 (Args) | `--port COM7 --screen 1920x1080` (one per line) |

Or import JSON via Settings → MCP → **从 JSON 导入**.

Cherry Studio routes MCP tools through whichever LLM provider is
selected (Qwen / DeepSeek / GLM / Claude / GPT-4 etc.) without
re-configuring MCP.

Official docs: <https://docs.cherry-ai.com/>

---

## Trae IDE

[Trae](https://www.trae.ai/) is ByteDance's AI-native IDE. Agent
supports stdio MCP transport.

Trae → **设置 (Settings)** → **MCP** → **添加 MCP 服务器**.

| Field | Value |
|-------|-------|
| 名称 (Name) | `clawtouch` |
| 传输方式 (Transport) | `stdio` |
| 命令 (Command) | `clawtouch-mcp` |
| 参数 (Args) | `--port COM7 --screen 1920x1080` |

Or import JSON; same shape as Claude Desktop's. Trae routes MCP tools
through whichever LLM the agent is bound to (default 豆包 / Doubao).

Official MCP overview (CN):
<https://www.w3cschool.cn/traedocs/trae-model-context-protocol.html>

---

## Troubleshooting

**Tool list empty / "Server failed to start":** run the command
manually in a terminal:
`clawtouch-mcp --port COM7 --screen 1920x1080 --log-level DEBUG`.
Most common cause: `clawtouch-mcp` not in PATH — use the absolute path
(`which` on Mac/Linux, `where` on Windows).

**Pico not detected:** `clawtouch-mcp` falls back to mock mode if it
can't find a Pico. Log line: *"no Pico device detected; falling back
to MOCK"*. Pass explicit `--port` to override.

**Coordinates clamped unexpectedly:** `--screen WxH` is doing its job.
Either remove the flag (coordinates pass through unclamped) or raise
the bounds to match your actual display.

**Mouse moves to bogus coordinates:** set `--screen WxH` to your
actual display so the agent can't move outside it. Keep
`hid.release_all` as a panic stop, or unplug the Pico if anything
goes sideways.
