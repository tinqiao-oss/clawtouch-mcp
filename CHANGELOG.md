# Changelog

All notable changes to `clawtouch-mcp` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions adhere to [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.5] — 2026-05-27 — Retina screenshot fix (real-world macOS report)

### Fixed — `hid.screenshot` overflow on high-DPI displays

A user testing the MCP server on Apple Silicon (logical 1512x982 /
physical 3024x1964, 2x scale) reported that every `hid.screenshot`
call truncated the result and the agent never saw the image. Root
cause was a units mismatch hiding a 4M-pixel cap:

```python
pixels = monitor["width"] * monitor["height"]   # mss returns LOGICAL on macOS
if pixels > MAX_SCREENSHOT_PIXELS:               # 1.48M < 4M, passes
    raise ValueError(...)
shot = sct.grab(monitor)                         # but grab returns PHYSICAL
png = mss.tools.to_png(shot.rgb, shot.size)      # PNG is 3024x1964 = 5.94M px
```

A ~3 MB base64 PNG then went into `{"content": [{"type": "text", ...}]}`
— the tool-result text envelope — and Claude Desktop / Claude Code
truncated it to a side file the agent couldn't read.

**Fix is architectural, not a wider cap:**

- **MCP image content type.** Screenshot tool returns an `ImageResult`
  marker which `_on_tool_call` translates into the spec-standard
  `{"type": "image", "data": ..., "mimeType": ...}` content entry.
  Clients route image content through their vision-token path, not
  the tool-result text buffer.
- **DPI-aware auto-resize.** Full-screen captures auto-downsample
  from physical pixels back to the configured logical screen size
  when the physical buffer is ≥1.2x bigger. On macOS Retina this
  collapses 3024x1964 → 1512x982; on Windows >100% DPI it collapses
  similarly; on Linux / 100% DPI it's a no-op. Pillow LANCZOS resize.
- **JPEG default.** New `format` param (`"jpeg"` / `"png"`, default
  `"jpeg"` at quality 80). Random-noise 1512x982 JPEG q80 ≤ 1 MB
  worst case; typical desktop content is ~150 KB.
- **Output-pixel cap.** `MAX_OUTPUT_PIXELS = 4_000_000` now measured
  on the *resized* image (not the raw mss grab), so it's a real
  defence against giant region requests instead of a no-op on Retina.
  Oversized requests are silently ratio-downsampled — agents see
  `width / height / raw_size` in metadata so they can tell.
- **Pillow added to `[screenshot]` extras.** `mss` still drives the
  capture; Pillow handles resize + JPEG encoding.

Behaviour change for callers: the result no longer has a `base64`
field at the top level. Image data flows through MCP image content;
metadata (`width / height / scale_x / scale_y / format / raw_size`)
flows through the sibling text content. Agents that read `scale_x` /
`scale_y` and divide screenshot coords keep working — the values
collapse to ~1.0 after the resize, so the division becomes a no-op.

`tests/test_screenshot_overflow.py` reproduces the Retina mismatch
with a mocked `mss` and pins all of the above (9 new tests).

## [0.2.4] — 2026-05-26 — Cumulative audit fixes (rounds 4–6)

This release rolls up audit work that landed since 0.2.3 — internal
4-agent round 4, multi-perspective round 5, and codex external
round 6. Each section below is preserved verbatim from the original
audit commits; the `## [Unreleased]` header was closed here.

### Fixed — internal deep audit (round 4)

A clean-up audit (four parallel agents, no specific external prompt)
on top of codex rounds 1-3 surfaced ~17 additional code-level
issues across server / bridge / CLI / examples. All P0 + P1 fixed in
this commit; P2/P3 stay in the backlog.

**P0 — MCP spec compliance**

- **`tools/call` exec errors now return `result.content + isError:true`,
  not JSON-RPC `-32000`.** Per MCP 2024-11-05 spec, JSON-RPC errors
  are reserved for protocol-layer faults; tool execution failures
  (rate limit, bridge timeout, hardware unavailable, validation
  errors, unknown tool name) must surface as `isError` content so
  the agent can read the message and react. Previously every
  `ValueError` / `RuntimeError` from a handler bubbled to
  `dispatch`'s `except Exception` and became a generic JSON-RPC
  error invisible to compliant clients (Claude Desktop, Cline).
  `_on_tool_call` now catches handler exceptions itself and includes
  the bridge's `last_error_detail` (timeout reason, seq mismatch,
  firmware ERROR code) inline. `unknown tool` likewise returns
  `isError` content listing the available tools.
- **Malformed JSON in stdio no longer crashes the server.** A single
  bad line (junk on stdout from a launcher script, BOM, blank `{`)
  used to raise `JSONDecodeError` at `json.loads(first)` /
  `json.loads(text)` in `run_stdio`, blow past the `except Exception`,
  and kill the session. Per JSON-RPC 2.0 spec, parse errors must
  return `{error: {code: -32700}}` and the connection should stay
  open. Now per-message `try/except json.JSONDecodeError` writes a
  -32700 response and continues.
- **Bridge ACK timeout no longer leaks stale bytes onto the next
  request.** `_send_raw` used to write straight to the serial line
  without flushing pyserial's input buffer; any residual bytes from
  a prior aborted request (a `0xAA` byte in payload coordinates,
  for example) could re-sync the parser onto mid-frame data and
  either fail checksum repeatedly or — worse — accept a stale ACK
  as the response for the new request (silently firing the wrong
  HID action). Now: `reset_input_buffer()` before every write, AND
  every response's `seq_id` is verified against the request's;
  mismatch is rejected as a stale ACK.
- **Windows DPI awareness now enabled unconditionally on server
  start.** Previously `SetProcessDpiAwareness(2)` only ran inside
  `_detect_screen` — when the user passed `--screen WxH`
  explicitly, the hook never fired, and on a 125%-scaled Windows
  host `GetCursorPos` returned logical (scaled) pixels while the
  `--screen` clamp was in physical pixels, so absolute clicks
  landed ~25% off. Now `_ensure_windows_dpi_awareness()` runs in
  `ClawTouchMcpServer.__init__` regardless of how `--screen` was
  resolved, keeping `cursor.py` and the clamp in the same
  coordinate space.

**P1 — server, bridge, CLI, examples**

- **`--screen` validation:** `0x0`, negative values, and malformed
  strings (`"1920x"`, `"1x2x3"`) used to either silently disable
  clamping (zero is falsy) or crash with an unhandled
  `ValueError`. Now `__main__.py` rejects all three with a clear
  `parser.error` message.
- **`--ops-per-sec` validation:** `0` or negative bricked every
  tool call (`initialize` and `tools/list` worked, every
  `tools/call` raised "rate limit exceeded"). Now rejected at the
  CLI with `parser.error`.
- **`hid.screenshot` region clamp + size cap:** an agent-supplied
  `region=[x1,y1,x2,y2]` with negative offsets or huge sizes used to
  capture across monitors the user may not have intended to expose,
  and a 4K×4K PNG (~30-80 MB base64) routinely OOMed the MCP client's
  JSON-RPC buffer. Now region is clamped to the primary monitor's
  bounds before grabbing, and `width × height > MAX_SCREENSHOT_PIXELS`
  (4M) returns a clear `ValueError` tool error.
- **`shutdown` method actually stops the server.** It used to return
  `{}` but never set `_stopping`, never closed the bridge, never
  broke `run_stdio`; clients saw the ack and stopped reading stdout,
  leaving the server blocked writing into a closed pipe. Now:
  `dispatch` sets `self._stop_event`, `run_stdio` checks the event
  on every loop turn and exits cleanly. Also handles
  `notifications/exit` for clients that prefer that path, and
  `notifications/cancelled` no-op so it's not flagged as unknown
  method.
- **Bridge IO failures now carry a diagnostic.** `_read_one_frame`
  used to return `None` for every failure (timeout / short header /
  short payload / parse error / mismatched seq) and the wrapping
  `mouse_*` / `key_*` methods returned a bare `ok=False` — the
  agent had no signal whether to retry, re-init, or escalate. Now
  each failure path sets `bridge.last_error_detail` with the
  specific reason, and `_on_tool_call` pulls it into the `isError`
  payload. Same hook surfaces firmware ERROR-frame responses with
  their `ErrorCode` name (`UNKNOWN_COMMAND` / `INVALID_PAYLOAD` /
  `CHECKSUM_MISMATCH` / `EXECUTION_TIMEOUT` / `DEVICE_BUSY`)
  instead of opaque ok=False. New `BridgeError` / `BridgeAckTimeout`
  / `BridgeAckMismatch` / `BridgeProtocolError` /
  `BridgeErrorResponse` exception classes exported from
  `clawtouch_mcp.bridge` so external bridge consumers can `try/except`
  the strongly-typed failure modes too.
- **`seq_id` 16-bit wrap now skips 0.** After 65535 ops the counter
  used to wrap to 0, colliding with the protocol's default
  `seq_id=0` on any frame built without an explicit seq. Long-
  running MCP sessions could see a stale default-seq ACK match a
  fresh request after wrap. Now `_next_seq` skips 0 on wrap.
- **`hid.type` strips control characters by default** (`\n`, `\r`,
  `\t`, `\x00`-`\x1f`, `\x7f`). An LLM agent drafting a multi-line
  message into a chat input would otherwise have its draft
  accidentally submitted by the `\n` being typed as Enter on the
  host. Pass `allow_control=True` to opt in to the raw byte stream
  (e.g. when intentionally driving a terminal app). Counts of
  stripped chars are logged at INFO so users notice.
- **`examples/computer_use/claude_demo.py` thinking + max_tokens
  contradiction fixed.** `thinking={"type":"adaptive"} + max_tokens
  =4096` is rejected by the Anthropic SDK (adaptive thinking
  requires `max_tokens` higher than the implicit thinking budget).
  Bumped to 16384 with an inline comment explaining the coupling
  with `model` and the `betas=` string.
- **`examples/computer_use/openai_cua_demo.py` scroll direction
  fixed.** The ternary `-(dy // 10) if dy > 0 else -(dy // 10)` had
  identical branches (never flipped sign) and Python's floor
  division of negatives over-scrolled upward (`-15 // 10 == -2`,
  not `-1`). Now `int(-dy / 10)` — single expression, correct
  rounding for both signs.
- **Dead `import io` removed from `claude_demo.py`.**
- **`examples/computer_use/README.md` `--ops-per-sec` line corrected.**
  Demo talks to `SerialHidBridge` directly (NOT through the MCP
  server), so the server's rate limiter is not in the loop —
  previous "default 10 in these demos" was simply wrong. README
  now says "pace tool calls yourself; add `asyncio.sleep` /
  `asyncio.Semaphore` if you need a cap".
- **Cross-repo wire protocol byte-equality test added.**
  `tests/test_cross_repo_protocol.py` (22 tests) compares every
  builder + enum value between `clawtouch_mcp.protocol` (this repo)
  and `clawtouch_hid_protocol.protocol` (the firmware repo) — the
  two packages independently implement the same frozen v1.0 wire
  format, and nothing else guards against silent drift. Skipped
  via `pytest.importorskip` when `clawtouch-hid-protocol` is not
  installed.
- **Test `_run(coro)` helpers no longer leak event loops.** Three
  test files used `asyncio.get_event_loop_policy().new_event_loop()
  .run_until_complete(coro)` and never closed the loop, producing
  ResourceWarning on Windows + orphan idle-watch tasks between
  tests. Now `try/finally` close.

**Tests:** 102 → **124** (added 16 cursor + 6 keycodes regression
guards in earlier commits, plus updated 2 dispatch tests for the new
`isError` contract). Cross-repo test suite adds another **22** when
`clawtouch-hid-protocol` is installed in dev mode.

### Fixed — absolute-coordinate semantics (codex round 3 P0/P1 #1)

- **`hid.click(x, y)` / `hid.move(x, y)` were not absolute.** Before
  this commit, `_tool_click` sent the raw target `(x, y)` to the
  firmware as a MOUSE_MOVE with `relative=False` flag set; the
  firmware's `_handle_mouse_move` ignored the flag entirely (USB HID
  Boot Mouse has no absolute-coordinate report) and treated `(x, y)`
  as a relative delta. An agent calling `hid.click(500, 300)` would
  see the cursor jump 500 px right and 300 px down from its current
  position, not land at the absolute (500, 300). Any Computer Use
  loop driving Claude Desktop / Cursor / Cline through this server
  would have mis-clicked on every call.

  **Fix architecture:** absolute coordinate semantics now live where
  they belong — on the host, not the firmware. New module
  `clawtouch_mcp/cursor.py` queries the OS for the current cursor
  position via `ctypes`:
    - Windows → `user32.GetCursorPos`
    - macOS   → `CoreGraphics.CGEventGetLocation` (via ctypes — no
      pyobjc dep)
    - Linux/X11 → `libX11.XQueryPointer`
    - Linux/Wayland → unsupported (no public unprivileged API);
      returns None deliberately
  `_tool_click` / `_tool_move` / `_tool_hover` now compute `(dx, dy)
  = (target - cursor)` and send a *relative* move that the firmware
  can actually execute. The firmware code path is unchanged and is
  now correctly documented as relative-only.

  **Failure path:** when the OS cursor query is unavailable (Wayland,
  unloadable libX11, GetCursorPos failure), the tool returns a
  structured error containing the platform-specific reason and the
  `relative=true` workaround — agents get a clear actionable message,
  not silent mis-clicks.

  **New `relative` parameter on `hid.click` / `hid.move`** lets an
  agent bypass the OS cursor query entirely and send raw pixel deltas
  — useful for headless / Wayland hosts and for sub-pixel scroll-like
  motion.

  **Test hook:** `CLAWTOUCH_FAKE_CURSOR=x,y` env var bypasses the OS
  query and returns the parsed coordinates instead, used by
  `tests/conftest.py` so the suite runs deterministically on headless
  CI without an X display.

  **Coverage:** new `tests/test_cursor.py` (16 tests) locks the env
  hook semantics, the delta math, the missing-cursor error path, the
  `relative=true` fast path, and `hid.move` / `hid.hover` parity.
  Total mcp test count: 118 (was 102).

  **README + tool descriptions** updated to spell out: default
  absolute via OS cursor query, `relative=true` opt-out, Wayland
  caveat, and the firmware-is-relative-only invariant.

### Fixed — second-pass code audit (codex round 3)

- **`examples/computer_use/claude_demo.py` — `ctrl+l` typed as bare
  'l' instead of triggering shortcut.** The "key" action fall-back
  used `key_name if not mods else key_name` (both branches identical),
  so any single-character key name with modifiers silently went to
  `bridge.type_text()` and missed the shortcut. Now only fall-back to
  `type_text` when there are no modifiers; with modifiers route to
  `bridge.key_combo(mods, key_name)` which can translate printable
  chars to keycodes, with a graceful `ValueError` catch for truly
  unknown keys.
- **`keycodes.py` missing punctuation-name aliases** like `plus`,
  `equal`, `minus`, `comma`, `period`, etc. — skill files (e.g.
  `clawtouch-skills/wps-office.md`) using
  `hid.key("ctrl+shift+plus")` would raise `ValueError unknown key:
  'plus'`. Added the common worded aliases so skills can reference
  punctuation by name; existing `=` / `+` literal usage still works.

### Terminology

- **Outward-facing copy: "LLM agent" → "AI agent"** in README hero,
  hero SVG alt text + diagram comment, `## What is this?`,
  Scope · Accessibility use case, `## About`, the Computer Use
  example README, and this changelog's own diagram description.
  Tracks the broader 2025 industry shift (Anthropic / OpenAI /
  Cursor / Cline now all default to "AI agent" in their public
  docs), and is what HN / GitHub / VC / B2B audiences search for.
- **Technical / compliance copy unchanged.** "LLM agent" is
  retained in: the `## Content generation` and `## Acceptable use`
  sections (legal precision — the LLM is the AI-content-generating
  party, not "any AI"), `SECURITY.md` (security-policy precision),
  `pyproject.toml` keyword comment (maintainer note), and the
  `clawtouch-skills` cross-link row on the Open source roadmap
  (matches the skills repo's internal wording, since markdown
  skills are LLM-specific by design — non-LLM agents have no use
  for prose prompts).

### Docs trim

- Removed redundant `🌐 clawtouch.cn` top-of-README link line —
  felt out-of-place above the badges (the same link still lives in
  the `## About` and `## License` sections).
- Removed the "🎥 a real screen-recording GIF will land here..."
  placeholder under `## See it in action`. The annotated stdio
  transcript stands on its own; no GIF promise to deliver on.
- Removed the "The dates aren't fixed — we ship when each piece is
  properly polished. Star the org..." sentence under
  `## Open source roadmap` — pure boilerplate, no information value.

### Visual / docs uplift

- **`docs/assets/hero.svg`** — flat-design hero diagram (AI agent →
  clawtouch-mcp → Pico 2 → target OS) embedded at the top of the
  English and Chinese READMEs. Highlights `clawtouch-mcp` as the
  "this repo" node and labels each transport hop (MCP stdio JSON-RPC
  / USB-CDC v1.0 frames / USB HID reports).
- **Architecture overview converted to Mermaid.** The previous
  ASCII box diagram in `## Architecture overview` is now a Mermaid
  `flowchart LR` with the `clawtouch-mcp` node highlighted (amber
  fill / thick border) as the this-repo marker. Renders natively on
  GitHub.
- **New `## See it in action` section.** An annotated stdio
  JSON-RPC transcript showing the full MCP `initialize` →
  `tools/list` → `tools/call` flow, with one `hid.click` and one
  `hid.type` call against a real Pico 2. Captured from
  `--log-level INFO` (USB serial randomized in the transcript). Acts
  as a text-based demo until a real screen-recording GIF lands.

### Compliance — second-pass audit (codex round 2)

A follow-up codex audit on the first compliance pass surfaced six
issues, all fixed below. The compliance scope is unchanged; wording
and packaging metadata are now stricter:

- **`## Acceptable use` reworded to scope-of-support, not a use
  restriction.** Replaced "you may not configure it to" with "this
  project does not support, document, or assist with". Added an
  explicit sentence that the section describes maintainer support
  scope only and is **not** an additional restriction on top of the
  MIT License's grant of code-level rights. Avoids the "MIT + use
  ban" structural conflict.
- **PRC Anti-Unfair Competition Law Art. 13 dating corrected.**
  Was "as amended 2025-10-15", which conflates promulgation and
  effective dates. Now reads "promulgated 2025-06-27, effective
  2025-10-15" (the latter is when the amendment takes effect, per
  the SPC publication). The substantive description was also
  broadened from the narrow "improper acquisition of others' data"
  to the statutory phrasing covering circumvention of technical
  management measures, fraud, and coercion as means.
- **`pyproject.toml` upgraded to PEP 639 license metadata.** Replaced
  `license = { text = "MIT" }` (deprecated table form) with
  `license = "MIT"` (SPDX expression). Added `license-files =
  ["LICENSE", "LICENSE.zh-CN.md", "NOTICE", "TRADEMARKS.md"]` so all
  four legal documents ship in the PyPI sdist/wheel `.dist-info/`
  directory. Bumped `setuptools>=77` (PEP 639 baseline). Removed the
  legacy `License :: OSI Approved :: MIT License` classifier per
  PyPA's PEP 639 migration guidance.
- **TRADEMARKS — owned-mark policy reworded to separate copyright
  and trademark grants.** The previous "non-commercial
  interoperability only" wording was ambiguous and could be read as
  restricting commercial use of the MIT-licensed code. Now states
  explicitly that MIT grants full commercial rights to the source
  code, that the marks are governed separately by trademark law,
  and that the only practical constraint on commercial forks is the
  trademark / naming requirement (rename, do not imply endorsement).
- **TRADEMARKS — official mark-owner attribution statements added.**
  New `### Official trademark attribution` subsection cites the
  attribution wording requested by Raspberry Pi Ltd., Adafruit
  Industries, Microsoft, Apple, Anthropic, and OpenAI per their
  respective trademark policies. Bilingual (English + 简体中文).

### Added
  and third-party marks referenced for descriptive purposes
  (Claude, OpenAI, Cursor, OpenClaw, Hermes, Cherry Studio, Trae IDE,
  Raspberry Pi, CircuitPython, Windows, macOS, etc.). PRC Trademark
  Law Art. 59 nominative-fair-use disclaimer included.
- **`LICENSE.zh-CN.md`** — non-official Chinese translation of the
  MIT License with explicit "English version prevails in case of
  conflict" disclaimer; references PRC open-source contract-law
  precedents (数字天堂诉柚子科技 / 罗盒诉风灵).
- **README `## Content generation — out of scope` section** —
  explicit declaration that this package does not generate any
  text/image/audio/video content, separating compliance scope from
  PRC *AI Generated Content Labeling Measure* (effective 2025-09-01)
  and the *Interim Measures for Generative AI Services*.
- **README `## Acceptable use` section** — explicit prohibition on
  bypassing target platforms' anti-fraud / risk-control / rate-limit
  measures and on operating accounts the user does not lawfully
  own; references PRC *Anti-Unfair Competition Law* Art. 13 (as
  amended 2025-10-15).
- **README License section** — added cross-links to
  `LICENSE.zh-CN.md`, `NOTICE`, and `TRADEMARKS.md`; clarified that
  MIT does not grant trademark rights.

### Changed

- **Test fixture USB serial numbers replaced with a synthetic value**
  (`E660000000000000`) instead of a real test-device serial; the
  affected test docstring rewritten to describe the technical
  reproduction scenario in neutral terms (no internal-date
  references).

### Fixed

- **`build_key_release()` now sends `[0x00, 0x00]` payload** instead of
  empty payload. Firmware `_handle_key_release` rejects frames with
  `len(payload) < 2` as `ERR_INVALID_PAYLOAD`, so `release_all()` was
  100% failing on real hardware. Spec ([protocol-v1.md §3.3](https://github.com/tinqiao-oss/clawtouch-hid/blob/master/docs/protocol-v1.md))
  says all-zero payload = release-all; the SDK now matches.
- `build_key_release()` gained optional `(keycode, modifiers)` params
  so a single key can be released too (backwards-compatible — bridge
  callers using `release_all()` keep working unchanged).
- Two new round-trip tests in `tests/test_protocol.py` lock the
  `[keycode, modifiers]` byte order so this can't silently regress.

### Changed

- **Docs / scope wording softened.** Rewrote scope paragraphs in both
  READMEs and `CONTRIBUTING.md` to describe HID input neutrally —
  driver-stack routing, no software on target. `docs/windows-setup.md`
  scope section trimmed for the same reason.
- `_detect_screen()` docstring + this changelog fixed to reflect that
  v0.2.3 actually uses `SM_CXSCREEN` / `SM_CYSCREEN` (primary monitor),
  not the `*VIRTUALSCREEN` variants — the docstring was wrong, the
  code was right.

### Removed

- `bridge._PICO_PIDS` constant — dead code. `likely_pico` detection
  only checks VID (line 89), the PID set was never read. Verified by
  grep + live `python -c "list_pico_ports()"` on a real Pico 2
  (which is PID `0x000B`, not in the old set, and was correctly
  flagged as `likely_pico=True` anyway). Both setup docs updated
  to stop referencing the removed constant.

## [0.2.3] — 2026-05-17 — Screen auto-detect + Windows setup guide

### Added

- **Auto-detect primary monitor's physical pixel size on startup** when
  `--screen` is not passed. Coordinates clamp to the real screen
  instead of the user having to guess. Implementation:
  - Windows: `ctypes.windll.user32.GetSystemMetrics(SM_CXSCREEN
    / SM_CYSCREEN)` (primary monitor) after `SetProcessDpiAwareness(2)`
    (or v1 fallback on pre-1809), so detection returns **physical**
    pixels regardless of display scaling.
  - macOS / Linux: `tkinter` (standard library — no extra dep).
  - All paths fail soft. If detection fails, the server logs a warning
    and runs with no clamping, same as if `--screen` was omitted
    pre-0.2.3.
- **`device.info` returns a new `screen` field** with `width`, `height`,
  and `source` (`"explicit"` / `"detected"` / `"unset"`). An MCP client
  can read this to know the active clamp bounds at runtime — no more
  guessing whether the agent's coordinate system matches the server's.
- **`docs/windows-setup.md`** (~250 lines) covers: VS Code Claude
  extension `.mcp.json` scope (NOT `~/.claude.json` top-level — the
  extension doesn't read it), full-window-restart requirement, dual
  COM port enumeration (VID `2E8A` PID `000B`), display-scaling and
  HID-coordinate relationship, multi-monitor `SM_CXSCREEN` (primary-
  only) semantics, and a real e2e Python script to validate end-to-end
  after install.
- 7 new tests in `tests/test_screen_detect.py` covering: explicit
  `--screen` beats detection / detection populates ServerConfig /
  detection failure → `source = "unset"` (no clamp) / partial-explicit
  still triggers detection / clamp uses detected bounds / no clamp when
  unset / real `_detect_screen()` returns `Optional[tuple[int,int]]`.
  Total test count: 68 (was 61).

### Changed

- `README.md` Run examples drop the hard-coded `--screen 1920x1080` —
  v0.2.3 doesn't need it. The README now points to both
  `docs/windows-setup.md` and `docs/macos-setup.md` upfront.

### Discovered

- Mismatched `--screen` is silent: a 5120×1440 super-wide screen with
  `--screen 1920x1080` clamps clicks to a 1920×1080 rectangle in the
  upper-left and **silently swallows** any click past those bounds.
  Found during Windows real-hardware bring-up of the Claude Code VS
  Code extension MCP integration. Auto-detect prevents this by default;
  agents can still pass `--screen` explicitly to clamp to a chosen
  monitor in multi-monitor setups.
- The VS Code Claude Code extension (2.1.143) reads `.mcp.json` at
  project root but **ignores `~/.claude.json` top-level `mcpServers`**
  even though the CLI honors it. This is documented in
  `docs/windows-setup.md` so future contributors don't repeat the same
  ~30-minute debug loop.

### Compatibility

- No breaking changes. Existing scripts that pass `--screen` continue
  to behave identically (explicit wins). Anyone that omitted `--screen`
  before now gets auto-clamp; pass `--screen` explicitly to force the
  old "no clamp at all" behavior is no longer possible without code
  changes — but it was never documented as intentional anyway, and
  auto-clamp is strictly safer.

## [0.2.2] — 2026-05-17 — Windows stdio asyncio P0 fix

### Fixed

- **Server completely unusable on Windows** in 0.2.0 / 0.2.1: the
  asyncio stdio reader used `loop.connect_read_pipe(sys.stdin)`, which
  the Windows `ProactorEventLoop` rejects (`CreateIoCompletionPort`
  refuses anonymous pipe handles → `OSError: [WinError 6]`). Any MCP
  client (Claude Desktop, Cursor, Cline, Claude Code, …) that spawned
  `clawtouch-mcp` on Windows hung the `initialize` handshake forever
  with no stdin processed and no useful error to the client. Discovered
  on Windows 11 Python 3.13 during MCP-client bring-up; not caught by
  mac/Linux validation because POSIX `SelectorEventLoop` supports
  `connect_read_pipe(stdin)`.
- `run_stdio` now reads stdin via `asyncio.to_thread(sys.stdin.buffer.readline)`
  on every platform — performance is fine for MCP traffic (single-digit
  req/s) and the code is now identical across OSes.

### Added

- `tests/test_stdio_integration.py` — 7 end-to-end stdio tests that
  spawn `python -m clawtouch_mcp --mock` as a real subprocess and
  exchange JSON-RPC over its pipes. The pre-0.2.2 unit tests all used
  the in-process `ClawTouchMcpServer` directly, so the stdio reader was
  never exercised under pytest — which is exactly why the Windows
  asyncio bug shipped. The new tests run on every platform in CI; the
  bug only reproduces on Windows but the regression guard is cheap.
  Total test count: 61 (was 54).

### Compatibility

- No API change. `auto_detect_port` / `SerialHidBridge` / wire protocol
  / config flags all unchanged from 0.2.1.
- No firmware update required.
- POSIX users see no behavior change — same JSON-RPC framing (line-
  delimited or `Content-Length`), same dispatch semantics. The internal
  reader switched from `asyncio.StreamReader` over a connected pipe to
  a thread-backed `readline`; user-visible behavior is identical.

## [0.2.1] — 2026-05-17 — Dual-CDC port detection fix

### Fixed

- **`auto_detect_port()` silently picked the REPL console instead of
  the data channel** on every Pico flashed with the standard ClawTouch
  firmware (`boot.py` enables `console=True, data=True`). The two CDC
  channels share VID/PID/serial_number, so the pre-0.2.1 logic
  returned whichever device pyserial listed first — typically the
  console — which then ignored every framed protocol byte and made
  `ping()` return `False` without an error. Discovered on a fresh
  Apple Silicon Mac mini during macOS bring-up (cu.usbmodem21201 vs
  21203).
- `_port_sort_key` does **natural** numeric ordering on the trailing
  port number, so `COM10` correctly sorts after `COM3` on Windows
  (lexicographic would invert them and pick the console).

### Added

- `is_data_port` field on each `list_pico_ports()` entry — `True`
  only for the highest-numbered port within each shared-serial
  group. Single-CDC firmwares degrade gracefully (sole port is
  marked data).
- 11 new tests covering: macOS dual CDC (`cu.usbmodem*`), Windows
  dual COM with two-digit numbers, Linux dual `/dev/ttyACM*`, single
  CDC, two Picos with distinct serials, and mixed Pico + non-Pico
  enumeration. Total test count: 54 (was 43).

### Compatibility

- `auto_detect_port()` return value changes for users who previously
  worked around the bug by passing `--port` explicitly to the data
  channel — they can now drop the flag. Anyone who happened to depend
  on the old (broken) behavior must now explicitly pass the lower-
  numbered console port via `--port`.
- No firmware update required. No hardware update required. The bug
  was always host-side.

## [0.2.0] — 2026-05-17 — First public release

First public release of the MCP server. Earlier internal builds existed
under the working name `openclaw-mcp` but were never published. The
0.x line is **stable for the v1.0 wire protocol** and the MCP
2024-11-05 protocol revision.

### Added

- **10 MCP tools** mapping LLM tool calls to HID primitives:
  `hid.click` / `hid.move` / `hid.hover` / `hid.type` / `hid.scroll`
  / `hid.key` / `hid.release_all` / `hid.screenshot` (opt-in) /
  `device.list` / `device.info`.
- **MockBridge** (`--mock`) for hardware-free development and CI.
- **Auto-detection** of Raspberry Pi Pico 2 boards via USB VID/PID; or
  explicit `--port`.
- **Safety rails**: coordinates clamped to `--screen WxH`, typed text
  capped at 4096 chars per call, rate-limited via `--ops-per-sec`.
- **Stdio framing** auto-detection (Content-Length vs. line-delimited
  JSON) — works with Claude Desktop, Cline, Continue, Cursor,
  [OpenClaw](https://github.com/openclaw), and
  [Hermes Agent](https://github.com/NousResearch/hermes-agent) out of
  the box.
- Test suite: 43 tests cover protocol round-trip, keycode mapping,
  dispatcher, rate limiter, and coordinate clamping.

### Known limitations

- Keyboard layout assumes US ABC. Hosts with a different system input
  method may see typed characters render as the wrong glyph; use
  `hid.key` for navigation and `hid.type` only on US-layout hosts.
- USB-CDC serial transport only; wireless transports are out of scope
  for this OSS release.
- No multi-touch HID profile yet — only mouse and keyboard.

[Unreleased]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/tinqiao-oss/clawtouch-mcp/releases/tag/v0.2.0
