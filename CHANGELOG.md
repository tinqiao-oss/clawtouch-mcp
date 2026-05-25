# Changelog

All notable changes to `clawtouch-mcp` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions adhere to [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
