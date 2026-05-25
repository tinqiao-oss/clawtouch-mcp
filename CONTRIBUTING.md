# Contributing to clawtouch-mcp

Thanks for your interest. This is a small project with a tightly-scoped
mission: **expose ClawTouch HID hardware as MCP tools, nothing more.**
Knowing what we will and won't take a PR for saves everyone's time.

## What we welcome

- **Bug fixes** with a regression test added under `tests/`.
- **New MCP tools** that wrap existing HID primitives in a more
  convenient shape (e.g. `hid.drag(from_x, from_y, to_x, to_y)` as a
  helper over the move + button-down + move + button-up dance).
  Include a test using `MockBridge`.
- **Client integration examples** in `examples/integrations/` — config
  snippets for new MCP clients (Goose, OpenDevin, your own agent).
  One file per client. Don't fork the main README to add a section.
- **Documentation improvements** — typos, clarifications, README
  translations (non-English).
- **Platform compatibility reports** — open an issue if the server
  doesn't run cleanly on your OS or with your MCP client; include logs
  and `clawtouch-mcp --mock --log-level DEBUG` output.

## What we won't take

- **Agent-loop logic and application-level features.** This is
  intentionally a thin HID primitive layer. Anything that decides
  *what* to do or *when* to do it belongs in your agent code, not
  here. We will close such PRs without review.
- **Application-specific adapters** (WeChat selectors, Discord
  shortcuts, etc.). Those live in agent / RPA frameworks built on top.
- **Wire-protocol changes.** The protocol is owned by the
  [clawtouch-hid](https://github.com/tinqiao-oss/clawtouch-hid)
  repository and frozen at v1.0. Open an issue there if you have a
  case for v2.
- **Vendored binaries / pre-built artifacts.** PyPI builds are the
  source of truth.

## Development setup

```bash
git clone https://github.com/tinqiao-oss/clawtouch-mcp
cd clawtouch-mcp
python -m venv .venv
.venv/bin/activate                              # or .venv\Scripts\activate on Windows
pip install -e ".[screenshot]"
pip install pytest
pytest tests/ -q
```

You can develop and test the full server without any hardware —
`--mock` mode is a complete replacement for the serial bridge.

## PR checklist

Before opening a PR:

- [ ] `pytest tests/ -q` passes locally.
- [ ] New behavior has a test (use `MockBridge` — no hardware needed).
- [ ] No new runtime dependencies unless absolutely necessary.
- [ ] CHANGELOG.md `[Unreleased]` section has a one-line entry.
- [ ] Commit messages are descriptive (>5 words; "fix bug" is not OK).

## Security

If you find a security issue (especially one that could let an attacker
bypass `--screen` clamping, `--ops-per-sec` rate limiting, or trigger
arbitrary HID output) please **do not** open a public issue.

See [SECURITY.md](SECURITY.md) for the private reporting process.

## License

By submitting a contribution you agree it is licensed under MIT (the
project license). No CLA, no copyright assignment. Your name stays on
your commits.
