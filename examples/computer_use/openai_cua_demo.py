# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""OpenAI CUA (Computer-Using Agent) → ClawTouch HID.

OpenAI's CUA model runs via the Responses API. The model returns
`computer_call` output items with actions; you execute them (here:
via ClawTouch HID) and feed the next screenshot back via
`computer_call_output` input items.

USAGE
    pip install clawtouch-mcp[screenshot] openai
    export OPENAI_API_KEY=sk-...

    python openai_cua_demo.py "Open my browser and search for ClawTouch"
    python openai_cua_demo.py --mock "..."
    python openai_cua_demo.py --port COM7 "..."

NOTE — CUA API is preview and shape may shift
    OpenAI's CUA endpoint, model name, and request/response shape are
    still moving (as of mid-2026). This demo uses the form documented
    when it was written; if the call shape errors, check OpenAI's
    current Computer Use docs:
    https://platform.openai.com/docs/guides/tools-computer-use

SAFETY CHECKS
    OpenAI CUA may return `pending_safety_check` items on a
    `computer_call`; per spec they must be echoed back as
    `acknowledged_safety_checks` on the next `computer_call_output`
    before the model will continue. By default this demo **aborts**
    when a pending safety check arrives — that is the safe default
    for a reference script. Pass `--acknowledge-safety-checks` to
    auto-ack and continue (use only in trusted sandbox environments;
    production deployments must surface the check to a human and
    only ack after explicit confirmation).

NOT INCLUDED
    - Multi-machine setups (screenshot stays local)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import sys
from typing import Any

from openai import AsyncOpenAI

from clawtouch_mcp.bridge import SerialHidBridge, auto_detect_port
from clawtouch_mcp.cursor import availability_hint, get_cursor_position
from clawtouch_mcp.keycodes import name_to_keycode
from clawtouch_mcp.server import MockBridge


# Closed-loop convergence constants — same as the server's snap-mode
# defaults (see clawtouch_mcp.server.MOVE_TOLERANCE / MOVE_MAX_ITERS /
# MOVE_SETTLE_MS). macOS pointer ballistics non-linearly scales single
# HID deltas (~110% in the low-speed segment), so a fire-and-forget
# move overshoots / undershoots by 10-90 px and leaves the cursor in
# the wrong UI cell for the subsequent click. We iterate until the
# residual is ≤5 px or we've burned 10 attempts — the loop early-exits
# the instant it's within tolerance, so 10 is a generous ceiling that
# only costs time on a struggling move (2-5 passes is the normal case).
_MOVE_TOLERANCE = 5
_MOVE_MAX_ITERS = 10
_MOVE_SETTLE_MS = 20


async def _move_to_absolute(bridge: Any, target_x: int, target_y: int) -> bool:
    """Translate a CUA screen-absolute coordinate into one or more
    relative bridge moves, converging on the target.

    The Pico firmware always interprets ``(x, y)`` as a relative pixel
    delta — USB Boot Mouse has no absolute-coordinate HID report, and
    ``firmware/code.py`` explicitly discards the ``relative`` flag.
    Calling ``bridge.mouse_move(x, y, relative=False)`` therefore
    silently sends the screen coordinate AS A DELTA, which sends the
    cursor flying off-screen on the first click. The MCP server's
    ``hid.click`` tool handles this conversion internally; demos that
    talk to the bridge directly must do it themselves.

    A single delta isn't enough on macOS: pointer ballistics scales
    the emitted delta non-linearly, so we query → emit → settle and
    repeat until residual ≤ ``_MOVE_TOLERANCE`` or ``_MOVE_MAX_ITERS``
    is exhausted. Returns ``True`` on convergence, ``False`` when the
    loop ran out of iterations (caller may inspect the OS cursor
    position itself to decide whether to retry). Raises if cursor
    tracking is unavailable on this host.
    """
    for _ in range(_MOVE_MAX_ITERS):
        current = get_cursor_position()
        if current is None:
            raise RuntimeError(
                "CUA absolute coordinates require OS cursor tracking, "
                "which is unavailable on this host. " + availability_hint()
            )
        dx = target_x - current[0]
        dy = target_y - current[1]
        if abs(dx) <= _MOVE_TOLERANCE and abs(dy) <= _MOVE_TOLERANCE:
            return True
        await bridge.mouse_move(dx, dy, relative=True)
        await asyncio.sleep(_MOVE_SETTLE_MS / 1000.0)
    return False

logger = logging.getLogger("clawtouch.cu.openai")


# ─────────────────────────── Action router ───────────────────────────

# OpenAI CUA action names differ slightly from Claude's; map to HID primitives.
_OAI_KEY_ALIAS = {
    "ENTER": "enter", "RETURN": "enter",
    "ESC": "escape", "ESCAPE": "escape",
    "TAB": "tab", "SPACE": "space",
    "BACKSPACE": "backspace", "DELETE": "delete",
    "UP": "up", "DOWN": "down", "LEFT": "left", "RIGHT": "right",
    "HOME": "home", "END": "end",
    "PAGEUP": "pageup", "PAGEDOWN": "pagedown",
    "CMD": "cmd", "WIN": "win", "META": "gui", "GUI": "gui",
    "CTRL": "ctrl", "SHIFT": "shift", "ALT": "alt",
}


def _take_screenshot_b64() -> str:
    # Full-resolution PNG of the primary monitor. Unlike the clawtouch-mcp
    # server (which downsamples via MAX_OUTPUT_PIXELS + LANCZOS), this
    # direct-bridge demo does not — a 4K / Retina frame can be several MB per
    # iteration. Add a resize step if payload size matters for your loop.
    try:
        import mss
        import mss.tools
    except ImportError:
        raise RuntimeError(
            "screenshot requires the [screenshot] extra: "
            "pip install 'clawtouch-mcp[screenshot]'"
        )
    # `mss.MSS` (uppercase) exists since mss 10.2; older 9.x/10.0/10.1 only
    # have `mss.mss`. The [screenshot] extra floors mss>=10.2, but stay
    # robust if an older mss is resolved transitively.
    _MSS = getattr(mss, "MSS", None) or mss.mss
    with _MSS() as sct:
        shot = sct.grab(sct.monitors[1])
        png = mss.tools.to_png(shot.rgb, shot.size)
    return base64.b64encode(png).decode("ascii")


async def execute(
    bridge: Any, action: dict, screen_w: int, screen_h: int
) -> None:
    """Dispatch one CUA action. Raises if the type is unknown."""
    t = action.get("type")

    if t == "click":
        x = max(0, min(int(action["x"]), screen_w - 1))
        y = max(0, min(int(action["y"]), screen_h - 1))
        await _move_to_absolute(bridge, x, y)
        button = action.get("button", "left")
        await bridge.mouse_click(button=button)
        return

    if t == "double_click":
        x = max(0, min(int(action["x"]), screen_w - 1))
        y = max(0, min(int(action["y"]), screen_h - 1))
        await _move_to_absolute(bridge, x, y)
        await bridge.mouse_click(button="left", double=True)
        return

    if t == "move":
        x = max(0, min(int(action["x"]), screen_w - 1))
        y = max(0, min(int(action["y"]), screen_h - 1))
        await _move_to_absolute(bridge, x, y)
        return

    if t == "type":
        await bridge.type_text(action["text"])
        return

    if t == "keypress":
        # OpenAI sends `keys` as a list, e.g. ["CTRL", "L"] or ["ENTER"]
        keys = action.get("keys") or []
        if not keys:
            return
        # Split modifiers from final key
        mods: list[str] = []
        for k in keys[:-1]:
            mod = _OAI_KEY_ALIAS.get(k.upper(), k.lower())
            mods.append(mod)
        final = keys[-1]
        key_name = _OAI_KEY_ALIAS.get(final.upper(), final.lower())
        if len(key_name) == 1 and not mods:
            await bridge.type_text(key_name)
            return
        if name_to_keycode(key_name) is None and len(key_name) == 1:
            # Letter with modifiers — bridge.key_combo handles it
            pass
        await bridge.key_combo(mods, key_name)
        return

    if t == "scroll":
        dy = int(action.get("scroll_y", 0))
        if dy:
            # CUA scroll_y is in pixels (positive = scroll down per CUA
            # convention); HID scroll is in wheel ticks (positive = up).
            # Convert with a rough factor (10 px ≈ 1 tick) and flip
            # sign so direction matches. Use int() of the float divide
            # rather than `//`, which on negatives floors toward
            # -infinity (e.g. -15 // 10 == -2, not -1) and would
            # over-scroll upward; the previous ternary had identical
            # branches anyway and never flipped sign at all.
            await bridge.mouse_scroll(int(-dy / 10))
        return

    if t == "wait":
        await asyncio.sleep(float(action.get("ms", 500)) / 1000.0)
        return

    if t == "screenshot":
        # Screenshot is captured separately and sent as `computer_call_output`
        return

    if t == "drag":
        # Real drag via the v1.1 button-hold primitives. CUA sends `path`:
        # a list of {x, y} points. Press at the first, glide through the
        # rest while held, release at the last. try/finally guarantees the
        # button is released even if a move raises. Mirrors hid.drag.
        path = action.get("path") or []
        pts = [(max(0, min(int(p["x"]), screen_w - 1)),
                max(0, min(int(p["y"]), screen_h - 1))) for p in path]
        if len(pts) < 2:
            logger.warning("drag needs >= 2 path points, got %d", len(pts))
            return
        await _move_to_absolute(bridge, *pts[0])
        await bridge.mouse_button_down(button="left")
        try:
            for x, y in pts[1:]:
                await _move_to_absolute(bridge, x, y)
        finally:
            await bridge.mouse_button_up(button="left")
        return

    logger.warning("unsupported action: %s", t)


# ─────────────────────────── Main loop ───────────────────────────

async def run(task: str, bridge: Any, screen_w: int, screen_h: int,
              max_iterations: int = 25, *,
              acknowledge_safety_checks: bool = False) -> None:
    """CUA Responses API loop: send → computer_call → execute → computer_call_output.

    Per OpenAI CUA spec, when a `computer_call` carries
    `pending_safety_checks`, the next `computer_call_output` must
    include `acknowledged_safety_checks` mirroring them. When
    ``acknowledge_safety_checks`` is False (default), this loop
    aborts on the first pending check — the safe default for a
    reference script. When True, the checks are echoed back so the
    model can continue.
    """
    client = AsyncOpenAI()

    # Initial request
    response = await client.responses.create(
        model="computer-use-preview",
        tools=[{
            "type": "computer_use_preview",
            "display_width": screen_w,
            "display_height": screen_h,
            "environment": "browser",  # or "windows" / "mac" / "linux"
        }],
        input=[{"role": "user", "content": task}],
        truncation="auto",
    )

    for iteration in range(max_iterations):
        logger.info("─── iteration %d ───", iteration + 1)

        # Find computer_call items in the response output
        calls = [item for item in response.output
                 if getattr(item, "type", None) == "computer_call"]

        # Surface any reasoning / text the model emitted
        for item in response.output:
            if getattr(item, "type", None) == "message":
                content = getattr(item, "content", [])
                for c in content:
                    if getattr(c, "type", None) == "output_text":
                        print(c.text)

        if not calls:
            logger.info("no computer_call in response; done")
            return

        # Execute each action, then capture screenshot, then send back
        inputs: list[dict] = []
        for call in calls:
            pending = list(getattr(call, "pending_safety_checks", None) or [])
            if pending and not acknowledge_safety_checks:
                logger.error(
                    "CUA returned %d pending_safety_check(s) — aborting. "
                    "Re-run with --acknowledge-safety-checks to opt into "
                    "automatic acknowledgement (trusted sandbox only).",
                    len(pending),
                )
                for c in pending:
                    logger.error("  safety check: code=%s message=%s",
                                 getattr(c, "code", "?"),
                                 getattr(c, "message", "?"))
                return

            try:
                await execute(bridge, call.action.model_dump()
                              if hasattr(call.action, "model_dump")
                              else dict(call.action),
                              screen_w, screen_h)
            except Exception as e:
                logger.exception("execute failed")

            # Always reply with a fresh screenshot — CUA expects it
            screenshot_b64 = _take_screenshot_b64()
            output_item: dict = {
                "call_id": call.call_id,
                "type": "computer_call_output",
                "output": {
                    "type": "computer_screenshot",
                    "image_url": f"data:image/png;base64,{screenshot_b64}",
                },
            }
            if pending:
                # Echo pending checks back per OpenAI CUA spec; without
                # this the model refuses to continue.
                output_item["acknowledged_safety_checks"] = [
                    {
                        "id": getattr(c, "id", None),
                        "code": getattr(c, "code", None),
                        "message": getattr(c, "message", None),
                    }
                    for c in pending
                ]
                logger.warning(
                    "auto-acknowledged %d safety check(s) — "
                    "--acknowledge-safety-checks is set",
                    len(pending),
                )
            inputs.append(output_item)

        # Continue the conversation
        response = await client.responses.create(
            model="computer-use-preview",
            previous_response_id=response.id,
            tools=[{
                "type": "computer_use_preview",
                "display_width": screen_w,
                "display_height": screen_h,
                "environment": "browser",
            }],
            input=inputs,
            truncation="auto",
        )

    logger.warning("hit max_iterations=%d", max_iterations)


# ─────────────────────────── Bridge wiring ───────────────────────────

async def make_bridge(mock: bool, port: str | None) -> Any:
    if mock:
        logger.info("MOCK mode — no hardware will move")
        return MockBridge()
    port = port or auto_detect_port()
    if not port:
        logger.warning("no Pico detected; falling back to MOCK")
        return MockBridge()
    bridge = SerialHidBridge(port)
    await bridge.connect()
    logger.info("connected to %s", port)
    return bridge


def parse_screen(s: str) -> tuple[int, int]:
    w, h = s.lower().split("x")
    return int(w), int(h)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("task")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--port")
    p.add_argument("--screen", default="1920x1080")
    p.add_argument("--max-iterations", type=int, default=25)
    p.add_argument("--log-level", default="INFO")
    p.add_argument(
        "--acknowledge-safety-checks",
        action="store_true",
        help=("Auto-acknowledge CUA pending_safety_checks (trusted sandbox "
              "only). Default: abort on first safety check."),
    )
    args = p.parse_args()

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not os.environ.get("OPENAI_API_KEY"):
        print("error: set OPENAI_API_KEY", file=sys.stderr)
        return 2

    screen_w, screen_h = parse_screen(args.screen)

    async def go():
        bridge = await make_bridge(args.mock, args.port)
        try:
            await run(
                args.task, bridge, screen_w, screen_h, args.max_iterations,
                acknowledge_safety_checks=args.acknowledge_safety_checks,
            )
        finally:
            await bridge.close()

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    sys.exit(main())
