# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Anthropic Claude Computer Use → ClawTouch HID.

Drives a real USB HID device (ClawTouch Pico) from Anthropic's Computer
Use loop. Single machine, single target — the agent and the OS it
controls are on the same PC. Screenshots come from `mss`; mouse and
keyboard actions go through `clawtouch_mcp.bridge.SerialHidBridge`.

USAGE
    pip install clawtouch-mcp[screenshot] anthropic
    export ANTHROPIC_API_KEY=sk-ant-...

    python claude_demo.py "Open my browser and search for ClawTouch"
    python claude_demo.py --mock "..."           # no hardware
    python claude_demo.py --port COM7 "..."      # explicit port

NOT INCLUDED
    - Multi-machine setups (screenshot path stays local)
    - `cursor_position` (HID is fire-and-forget)
    - Production-grade safety rails — see README.md
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import sys
from typing import Any

import anthropic

# Direct bridge import — these demos talk to the HID device without
# going through the clawtouch-mcp server subprocess.
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
# residual is ≤3 px or we've burned 4 attempts.
_MOVE_TOLERANCE = 3
_MOVE_MAX_ITERS = 4
_MOVE_SETTLE_MS = 20


async def _move_to_absolute(bridge: Any, target_x: int, target_y: int) -> bool:
    """Translate a Computer Use screen-absolute coordinate into one
    or more relative bridge moves, converging on the target.

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
                "Claude Computer Use absolute coordinates require OS cursor "
                "tracking, which is unavailable on this host. "
                + availability_hint()
            )
        dx = target_x - current[0]
        dy = target_y - current[1]
        if abs(dx) <= _MOVE_TOLERANCE and abs(dy) <= _MOVE_TOLERANCE:
            return True
        await bridge.mouse_move(dx, dy, relative=True)
        await asyncio.sleep(_MOVE_SETTLE_MS / 1000.0)
    return False

logger = logging.getLogger("clawtouch.cu.claude")


# ─────────────────────────── Action router ───────────────────────────

# Claude's `key` action sends strings like "Return", "ctrl+l", "shift+Tab".
# Translate them to (modifiers, key_name) the HID bridge understands.
_KEY_ALIAS = {
    "return": "enter", "enter": "enter",
    "escape": "escape", "esc": "escape",
    "backspace": "backspace", "delete": "delete",
    "tab": "tab", "space": "space",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end",
    "page_up": "pageup", "pageup": "pageup",
    "page_down": "pagedown", "pagedown": "pagedown",
}


def _parse_key(text: str) -> tuple[list[str], str]:
    """'ctrl+shift+l' -> (['ctrl', 'shift'], 'l').  'Return' -> ([], 'enter')."""
    parts = [p.strip().lower() for p in text.split("+")]
    mods, key = parts[:-1], parts[-1]
    return mods, _KEY_ALIAS.get(key, key)


def _take_screenshot() -> dict:
    """Return a Claude-shaped image content block.

    Note: this sends a full-resolution PNG of the primary monitor. The
    clawtouch-mcp *server* downsamples screenshots (MAX_OUTPUT_PIXELS +
    LANCZOS) to keep MCP buffers small; this direct-bridge demo does not,
    so on a 4K / Retina display the per-iteration payload can be several
    MB. Add a resize step if that matters for your loop.
    """
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
        shot = sct.grab(sct.monitors[1])  # primary monitor
        png_bytes = mss.tools.to_png(shot.rgb, shot.size)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png_bytes).decode("ascii"),
        },
    }


async def execute_action(
    bridge: Any, action: str, params: dict, screen_w: int, screen_h: int
) -> Any:
    """Dispatch one Computer Use action. Returns content for tool_result."""
    if action == "screenshot":
        return [_take_screenshot()]

    if action == "mouse_move":
        x, y = params["coordinate"]
        x = max(0, min(x, screen_w - 1))
        y = max(0, min(y, screen_h - 1))
        await _move_to_absolute(bridge, x, y)
        return "moved"

    if action in ("left_click", "right_click", "middle_click", "double_click"):
        # If coordinate given, move first
        if "coordinate" in params:
            x, y = params["coordinate"]
            x = max(0, min(x, screen_w - 1))
            y = max(0, min(y, screen_h - 1))
            await _move_to_absolute(bridge, x, y)
        button = {"left_click": "left", "right_click": "right",
                  "middle_click": "middle", "double_click": "left"}[action]
        await bridge.mouse_click(button=button, double=(action == "double_click"))
        return f"{action}"

    if action == "type":
        await bridge.type_text(params["text"])
        return f"typed {len(params['text'])} chars"

    if action == "key":
        mods, key_name = _parse_key(params["text"])
        keycode = name_to_keycode(key_name)
        if keycode is None and len(key_name) == 1 and not mods:
            # No modifiers and no named keycode — fall back to type_text.
            # (With modifiers we MUST stay in key_combo so the bridge can
            #  press the modifier + emit the key as a HID report, otherwise
            #  e.g. "ctrl+l" would type a bare 'l' and miss the shortcut.)
            await bridge.type_text(key_name)
            return "typed (no keycode)"
        if keycode is None:
            # Single char + modifiers: let the bridge handle the combo —
            # key_combo translates printable chars to their keycodes too.
            try:
                await bridge.key_combo(mods, key_name)
                return f"pressed {params['text']}"
            except ValueError as e:
                return f"unknown key: {params['text']} ({e})"
        await bridge.key_combo(mods, key_name)
        return f"pressed {params['text']}"

    if action == "scroll":
        # Claude sends: scroll_direction ("up"/"down"/"left"/"right") + scroll_amount
        direction = params.get("scroll_direction", "down")
        amount = int(params.get("scroll_amount", 3))
        delta = amount if direction == "up" else -amount
        await bridge.mouse_scroll(delta)
        return f"scrolled {direction} {amount}"

    if action in ("left_mouse_down", "left_mouse_up"):
        # v1.1 button-hold primitives (CUA left_mouse_down / left_mouse_up).
        if "coordinate" in params:
            x, y = params["coordinate"]
            await _move_to_absolute(bridge, max(0, min(x, screen_w - 1)),
                                    max(0, min(y, screen_h - 1)))
        if action == "left_mouse_down":
            await bridge.mouse_button_down(button="left")
        else:
            await bridge.mouse_button_up(button="left")
        return action

    if action == "left_click_drag":
        # Real drag via the v1.1 button-hold primitives: press at the
        # start, glide to the end while held, release. Mirrors the MCP
        # server's hid.drag (server._tool_drag); the try/finally
        # guarantees the button is released even if the move raises.
        end = params["coordinate"]
        ex = max(0, min(end[0], screen_w - 1))
        ey = max(0, min(end[1], screen_h - 1))
        start = params.get("start_coordinate")
        if start is not None:
            await _move_to_absolute(bridge, max(0, min(start[0], screen_w - 1)),
                                    max(0, min(start[1], screen_h - 1)))
        await bridge.mouse_button_down(button="left")
        try:
            await _move_to_absolute(bridge, ex, ey)
        finally:
            await bridge.mouse_button_up(button="left")
        return "left_click_drag"

    if action == "cursor_position":
        # HID is fire-and-forget — we don't track cursor state
        return [{"type": "text", "text": "0, 0"}]

    return f"unsupported action: {action}"


# ─────────────────────────── Main loop ───────────────────────────

async def run(task: str, bridge: Any, screen_w: int, screen_h: int,
              max_iterations: int = 25, model: str = "claude-opus-4-8") -> None:
    """Standard Computer Use loop: send → tool_use → execute → tool_result → repeat."""
    client = anthropic.AsyncAnthropic()
    messages: list[dict] = [{"role": "user", "content": task}]

    for iteration in range(max_iterations):
        logger.info("─── iteration %d ───", iteration + 1)
        # Notes on the request shape:
        # - `max_tokens=16384`: adaptive extended-thinking requires a
        #   higher cap than the default 4096 (the SDK enforces a floor
        #   above the implicit thinking budget). Use 16K as a safe
        #   default for Computer Use loops.
        # - `thinking={"type":"adaptive"}`: lets Claude decide how much
        #   reasoning budget each step needs. Drop this field if your
        #   model variant doesn't expose extended thinking.
        # - `model` and `betas` are coupled — the beta string changes
        #   with each Computer Use model release. **Verify the current model
        #   + beta in Anthropic's Computer Use docs before running**: model
        #   IDs are retired over time and a stale pin returns 404. Override
        #   with `--model`; the default tracks the current GA Opus.
        async with client.beta.messages.stream(
            model=model,
            max_tokens=16384,
            thinking={"type": "adaptive"},
            tools=[{
                "type": "computer_20250124",
                "name": "computer",
                "display_width_px": screen_w,
                "display_height_px": screen_h,
                "display_number": 1,
            }],
            messages=messages,
            betas=["computer-use-2025-01-24"],
        ) as stream:
            response = await stream.get_final_message()

        # Surface what Claude said
        for block in response.content:
            if block.type == "text":
                print(block.text)

        # Stop conditions
        if response.stop_reason == "end_turn":
            logger.info("Claude finished naturally")
            return

        # Append assistant turn verbatim (must preserve thinking blocks)
        messages.append({"role": "assistant", "content": response.content})

        # Process every tool_use block; one tool_result per tool_use
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            params = dict(block.input)
            action = params.pop("action", "")
            try:
                result = await execute_action(
                    bridge, action, params, screen_w, screen_h)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result if isinstance(result, list) else
                               [{"type": "text", "text": str(result)}],
                })
            except Exception as e:
                logger.exception("action %s failed", action)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": [{"type": "text", "text": f"error: {e}"}],
                    "is_error": True,
                })

        if not tool_results:
            logger.info("no tool calls; stopping")
            return

        messages.append({"role": "user", "content": tool_results})

    logger.warning("hit max_iterations=%d without end_turn", max_iterations)


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
    p.add_argument("task", help="Task for Claude in plain English")
    p.add_argument("--mock", action="store_true",
                   help="No hardware — actions are logged but nothing moves")
    p.add_argument("--port", help="Serial port (auto-detect if omitted)")
    p.add_argument("--screen", default="1920x1080",
                   help="WIDTHxHEIGHT for coordinate clamping (default 1920x1080)")
    p.add_argument("--max-iterations", type=int, default=25,
                   help="Loop safety limit (default 25)")
    p.add_argument("--model", default="claude-opus-4-8",
                   help="Anthropic model ID (verify the current Computer Use model in Anthropic docs)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: set ANTHROPIC_API_KEY", file=sys.stderr)
        return 2

    screen_w, screen_h = parse_screen(args.screen)

    async def go():
        bridge = await make_bridge(args.mock, args.port)
        try:
            await run(args.task, bridge, screen_w, screen_h, args.max_iterations, args.model)
        finally:
            await bridge.close()

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    sys.exit(main())
