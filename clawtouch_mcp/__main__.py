"""Entry point: `python -m clawtouch_mcp` or `clawtouch-mcp` script."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .server import ClawTouchMcpServer, ServerConfig, run_stdio


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="clawtouch-mcp",
        description="MCP stdio server exposing HID tools backed by a Pico 2 board.",
    )
    parser.add_argument("--port", help="Serial port, e.g. COM7 or /dev/ttyACM0. Auto-detect if omitted.")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--screen", metavar="WxH", help="Clamp coordinates, e.g. 1920x1080")
    parser.add_argument("--ops-per-sec", type=float, default=20.0)
    parser.add_argument("--mock", action="store_true", help="Do not touch hardware; log calls only.")
    parser.add_argument("--allow-screenshot", action="store_true",
                        help="Enable the hid.screenshot tool (requires mss).")
    parser.add_argument("--idle-close-after", type=float, default=30.0,
                        help="Release the COM port after this many seconds with "
                             "no tool call (default 30; 0 disables). Lets other "
                             "processes (e.g. ClawTouch desktop) acquire the same "
                             "board without manual kill. Next tool call lazy-"
                             "reconnects (~50-200ms overhead).")
    parser.add_argument("--log-level", default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    # NOTE: log to stderr — stdout is reserved for JSON-RPC frames.
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # --ops-per-sec: 0 or negative would brick every tool call. Was
    # silent before — server accepted initialize / tools/list then
    # rejected every tools/call with "rate limit exceeded".
    if args.ops_per_sec <= 0:
        parser.error(
            f"--ops-per-sec must be positive (got {args.ops_per_sec})"
        )

    cfg = ServerConfig(
        port=args.port,
        baudrate=args.baudrate,
        ops_per_sec=args.ops_per_sec,
        mock=args.mock,
        allow_screenshot=args.allow_screenshot,
        idle_close_after=args.idle_close_after,
    )
    if args.screen:
        # --screen used to crash on "1920x" (int("") raises outside the
        # try) or silently disable clamping on "0x0" (zero is falsy).
        # Validate explicitly so the user gets a clear error.
        try:
            parts = args.screen.lower().split("x")
            if len(parts) != 2:
                raise ValueError("must be exactly one 'x' separator")
            w, h = int(parts[0]), int(parts[1])
        except (ValueError, TypeError) as e:
            parser.error(
                f"invalid --screen {args.screen!r}, expected e.g. 1920x1080 "
                f"({e})"
            )
        if w <= 0 or h <= 0:
            parser.error(
                f"invalid --screen {args.screen!r}: width and height must be "
                f"positive (got {w}x{h})"
            )
        cfg.screen_w, cfg.screen_h = w, h

    async def _run() -> None:
        server = ClawTouchMcpServer(cfg)
        try:
            await server.start()
            await run_stdio(server)
        finally:
            await server.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
