# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""MCP stdio server exposing the ClawTouch HID input + device tool set.

Implements the subset of MCP spec (2024-11-05 / 2025-03-26) needed by
OpenClaw, Hermes, Claude Desktop, Cline, etc.:

    initialize                (handshake)
    notifications/initialized (no-op)
    tools/list
    tools/call
    ping
    shutdown

Everything flows over stdio as Content-Length framed or line-delimited
JSON-RPC 2.0. We support both framings (line-delimited is the default
used by Claude Desktop; Content-Length framing is used by some IDE hosts).

Safety:
    * Coordinates clamped to screen bounds (if `--screen WxH` provided).
    * Typed text length capped at MAX_TYPE_LEN chars.
    * Rate-limited by configurable ops/sec.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import __version__
from .bridge import SerialHidBridge, auto_detect_ports, list_pico_ports
from . import cursor as _cursor_mod
from .cursor import (
    _seed_fake_cursor,
    _update_fake_cursor,
    availability_hint,
    get_cursor_position,
)

logger = logging.getLogger("clawtouch_mcp.server")

MCP_PROTOCOL_VERSION = "2024-11-05"
MAX_TYPE_LEN = 4096
# Upper bound on the optional ``move_ms`` argument (hid.click / hid.move /
# hid.hover). Path stepping over more than 5 s blocks a tool handler
# longer than any reasonable demo needs, so we cap rather than let an
# agent / typo lock up the server.
MAX_MOVE_MS = 5000
# hid.batch caps. The op count cap is the primary safety rail for the
# batch tool: this drives REAL keyboard/mouse on the host, so a large
# blind burst (LLM hallucination / prompt injection feeding a huge
# array) would fire that many real actions at the frontmost window with
# no chance to interrupt — stdio is strictly serial, so while a batch
# runs NO other tool call (not even hid.release_all to stop it) can get
# in. A small cap keeps any single batch short-lived; the OSS stdio
# server has no global F9 panic key / status bar to fall back on. The
# server enforces this in the handler (NOT just schema maxItems) because
# a raw JSON-RPC client can bypass client-side schema validation.
#   - MAX_BATCH_OPS=10: enough for a pre-computed action list (e.g. a
#     minesweeper solver emitting several fixed coordinates); more than
#     that is better expressed as separate observe-decide-act calls.
#   - MAX_BATCH_DELAY_MS=2000: per-op post-delay ceiling, so a stray
#     delay_ms can't pin the serial transport for an unbounded time
#     (worst case 10 * 2 s = 20 s head-of-line, deliberately bounded).
MAX_BATCH_OPS = 10
MAX_BATCH_DELAY_MS = 2000
# Default inter-op settle inserted AFTER click/button ops when the caller
# did not set delay_ms. Real macOS dogfood (native Minesweeper,
# 2026-06-04): a hid.batch of vertically-adjacent clicks fired back-to-back
# with zero gap had only the LAST click register — the OS/app coalesced or
# dropped the earlier ones even though every HID click was sent and ACKed
# (clicked:true). The HID layer can't observe an app-level drop, so the fix
# is to PACE discrete clicks. ~40 ms made the repro 100% reliable; 50 ms is
# the default with margin. An explicit delay_ms (including 0, for advanced
# callers who want none) overrides it; non-click ops default to 0 as before.
DEFAULT_CLICK_SETTLE_MS = 50
_SETTLE_OP_TYPES = frozenset({"click", "button_down", "button_up"})
# Closed-loop convergence constants for absolute cursor moves.
# macOS pointer ballistics non-linearly scales single HID deltas
# (measured ~110% in low-speed segment on Ventura ARM64), so a single
# fire-and-forget mouse_move overshoots/undershoots by 10-90 px and
# returns ok=true while the cursor is still drifting. We iterate:
# query OS cursor → compute residual → send delta → settle → repeat.
# Per-pass residual shrinks to ~30% of previous (empirically), so the
# loop converges geometrically toward the ±2 px report-quantization
# floor. The loop EARLY-EXITS the instant residual ≤ MOVE_TOLERANCE,
# so MOVE_MAX_ITERS is a generous *ceiling*, not a fixed budget: a
# normal move converges in 2-5 passes regardless of the cap, and the
# headroom only costs wall-clock on a genuinely struggling move (stuck
# cursor / competing input). This makes accuracy independent of move
# distance and screen size with no per-distance calibration.
#   - MOVE_TOLERANCE=5 px: comfortably above macOS's ±2 px report
#     quantization, so the loop reliably TERMINATES as converged at the
#     floor instead of oscillating on intrinsic jitter (tol=3 sat right
#     on the jitter band). Still far inside any clickable target
#     (smallest common UI ~16 px).
#   - MOVE_MAX_ITERS=10: generous ceiling (early-exit keeps the common
#     case at 2-5 passes). Was 4, calibrated to specific test targets;
#     that proved too tight once the glide path (_stepped_move_to_absolute)
#     spent a pass on its post-slide residual and left a 4-7 px near-miss
#     the click gate then refused (real-hardware mac dogfood 2026-06-04).
#   - MOVE_SETTLE_MS=20: ~2× macOS HID report cycle (8-10 ms).
MOVE_TOLERANCE = 5
MOVE_MAX_ITERS = 10
MOVE_SETTLE_MS = 20
# Death-spiral guard for the move loops (_converge_to_target /
# _stepped_move_to_absolute / _stepped_relative_move). A dead or unplugged
# device never ACKs a mouse report, and each un-ACKed report blocks the
# bridge for the full per-ACK timeout (SerialHidBridge default 1.0 s). With
# no guard, a glide can fire up to 100 such reports plus 10 converge passes
# → ~110 s of dead-air for ONE move, and a continue-on-error hid.batch
# multiplies that by the op count (~19 min for 10 ops) — all while stdio is
# strictly serial, so not even hid.release_all can get in. We bail after
# this many CONSECUTIVE un-ACKed reports. The counter resets on any ACK, so
# a transient single drop on a live device still rides through; only a
# sustained run (= the device is gone) trips it, collapsing the dead-device
# case to ~N × timeout. MAX_MOVE_MS bounds the intended glide *sleeps*; this
# bounds the *ACK-wait* dead-air the move_ms cap never covered.
MAX_CONSECUTIVE_MOVE_TIMEOUTS = 3
# Upper bound on the Content-Length header of an incoming framed
# JSON-RPC message. The MCP spec allows arbitrary message sizes but in
# practice every reasonable tool call fits in well under 1 MB; capping
# at 16 MB keeps a single bad/malicious header from making _read_exact
# allocate gigabytes before EOF. Returns -32700 parse error on overrun.
MAX_FRAME_LEN = 16 * 1024 * 1024
_MODIFIER_NAMES = frozenset({"ctrl", "shift", "alt", "gui", "win", "cmd"})

# Quit/close key combos that self-interrupt when this MCP server shares a
# machine with the agent driving it. Real USB HID has no app-level
# addressing — keystrokes land on whatever window is frontmost — so if the
# agent app (Claude Code / Cursor / ChatGPT Desktop) is focused, cmd+q /
# alt+f4 quit the agent itself mid-task (lost context, unrecoverable).
# Detecting focus needs OS-specific window introspection that we keep out
# of this portable, near-dependency-free server, so instead of blocking we
# emit one best-effort heads-up the first time such a combo is sent. Never
# blocks, never swallows the keystroke (a remote/cross-device target makes
# the same combo perfectly legitimate). See INTEGRATIONS.md "known footgun".
_GUI_MODIFIERS = frozenset({"gui", "win", "cmd"})  # all map to the GUI/Command key


def _is_self_interrupt_combo(modifiers: list[str], key: str) -> bool:
    mods = {m.lower() for m in modifiers}
    k = key.strip().lower()
    if k == "q" and (_GUI_MODIFIERS & mods):
        return True  # cmd+Q  -> macOS "Quit application"
    if k == "f4" and "alt" in mods:
        return True  # alt+F4 -> Windows "Close application"
    return False


def _ensure_windows_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness for this process on Windows.

    GetCursorPos / GetSystemMetrics only return true physical pixels
    when the calling process is DPI-aware. We MUST call this before
    either `_detect_screen` (which feeds the clamp bounds) or
    `cursor.get_cursor_position` (which underlies absolute clicks),
    otherwise the two coordinate systems can disagree under display
    scaling and an absolute hid.click lands ~25% off on a 125% host.

    Idempotent — second/Nth calls are no-ops. Fail-soft: on macOS /
    Linux / when both Windows entry points are missing, returns
    silently. No exception propagates to the caller.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # Try the modern per-monitor-v2 awareness first; fall back to
        # the older v1 API on pre-1809 Windows; ignore failures (Wine,
        # locked-down kiosks, …).
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    except Exception:
        pass


def _detect_screen() -> Optional[tuple[int, int]]:
    """Detect the primary monitor's physical pixel size, cross-platform.

    Used when the user did not pass --screen WxH explicitly. Returns
    (width, height) or None when detection fails.

    Windows: ctypes user32.GetSystemMetrics(SM_CXSCREEN /
    SM_CYSCREEN) for the primary monitor, with SetProcessDpiAwareness
    so we get true physical pixels even under display scaling.
    mac/Linux: tkinter (standard library, no extra deps). All paths
    fail soft."""
    try:
        if sys.platform == "win32":
            import ctypes
            # DPI awareness — separately ensured by ClawTouchMcpServer.__init__
            # so cursor.py also benefits. Calling it here too is harmless and
            # keeps _detect_screen self-contained for standalone use.
            _ensure_windows_dpi_awareness()
            # Use primary monitor (SM_CXSCREEN/SM_CYSCREEN) rather than
            # the virtual screen bounding box across all monitors —
            # hid.screenshot defaults to capturing the primary monitor
            # via mss, so clamping to the same rectangle keeps screenshot
            # coordinates and click coordinates consistent. Users with
            # multi-monitor setups who want broader clamp bounds should
            # pass --screen WxH explicitly.
            user32 = ctypes.windll.user32
            w = user32.GetSystemMetrics(0)  # SM_CXSCREEN (primary monitor)
            h = user32.GetSystemMetrics(1)  # SM_CYSCREEN
            if w > 0 and h > 0:
                return (int(w), int(h))
            return None
        import tkinter as tk
        root = tk.Tk()
        try:
            w = root.winfo_screenwidth()
            h = root.winfo_screenheight()
            return (int(w), int(h)) if w > 0 and h > 0 else None
        finally:
            root.destroy()
    except Exception as e:
        logger.debug("screen auto-detect failed: %s", e)
        return None


# ════════════════════════════════════════════════════════════════════
# Tool-selection guidance (prepended to every hid.* tool description)
# ════════════════════════════════════════════════════════════════════
# LLMs should prefer hid.* tools when:
#   (1) no API / automation path exists for the target application, or
#   (2) the user explicitly requests physical keyboard / mouse input.
# This prefix is visible at tool-selection time even if the client
# ignores the server-level `instructions` field returned by initialize.
HID_PREFIX = (
    "[Physical HID input — pick this when other automation paths "
    "(file APIs, browser automation, OS APIs) cannot accomplish the "
    "task, or when the user explicitly requests physical keyboard "
    "or mouse input.] "
)

# ═══════════════════ Tool registry ═══════════════════

@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Awaitable[dict]]


@dataclass
class ImageResult:
    """Tool handler return marker for image payloads.

    The dispatch layer (``_on_tool_call``) translates this into the
    MCP-standard ``{"type": "image", "data": <base64>, "mimeType": ...}``
    content entry. Image content flows through vision-token paths in
    MCP clients (Claude Desktop / Claude Code) rather than the
    tool-result text buffer — necessary because a Retina-resolution
    PNG base64 easily exceeds the text envelope and gets truncated.

    The accompanying ``metadata`` dict is rendered as a sibling text
    content entry so the agent still sees width/height/scale_x/etc.
    """
    image_bytes: bytes
    mime_type: str
    metadata: dict


@dataclass
class ServerConfig:
    screen_w: Optional[int] = None
    screen_h: Optional[int] = None
    ops_per_sec: float = 20.0
    port: Optional[str] = None
    baudrate: int = 115200
    mock: bool = False
    allow_screenshot: bool = False
    # screenshot encode backend: 'auto' (Pillow when its native _imaging
    # loads, else the no-native 'mss-png' path) | 'pillow' | 'mss-png'.
    # mss-png needs no compiled extension → works under hardened-runtime
    # library-validation Python hosts that reject Pillow's _imaging.so.
    screenshot_backend: str = "auto"
    # release-on-idle: 若 idle_close_after 秒内无 tools/call → close 串口
    # (替换为 UnavailableBridge), 让其他进程 (如 ClawTouch desktop) 拿到
    # 同一块板. 下次 tools/call 通过 UnavailableBridge._try_promote 自动
    # 重连. 0 = 关闭此功能, 长占串口 (旧行为).
    idle_close_after: float = 30.0
    # _idle_watch 多久醒来检查一次; 0 = auto (基于 idle_close_after 算).
    # 默认 auto: max(1, min(5, idle_close_after/6)) — 平衡精度跟 CPU 开销.
    idle_check_interval: float = 0.0


@dataclass
class RateLimiter:
    ops_per_sec: float
    _window: list[float] = field(default_factory=list)

    def check(self) -> None:
        now = time.monotonic()
        self._window = [t for t in self._window if now - t < 1.0]
        if len(self._window) >= self.ops_per_sec:
            raise RuntimeError(
                f"rate limit exceeded ({self.ops_per_sec:.0f} ops/sec)"
            )
        self._window.append(now)


class MockBridge:
    """In-memory bridge for `--mock` mode; logs but never touches
    hardware. Lazily seeds the cursor dynamic state on first
    ``mouse_move`` (from ``CLAWTOUCH_FAKE_CURSOR`` env or a default
    of 960,540) and updates it on every subsequent move, so the
    server's closed-loop converge path sees the cursor land where
    the firmware was told to go. Real hardware does this via the
    OS event loop; mock has to fake it for converge to terminate."""

    def __init__(self) -> None:
        self.is_connected = True
        self.port = "<mock>"
        self.baudrate = 0
        self._calls: list[tuple[str, dict]] = []

    async def connect(self) -> None:  # pragma: no cover - trivial
        return None

    async def close(self) -> None:  # pragma: no cover - trivial
        self.is_connected = False

    async def ping(self) -> bool:
        self._calls.append(("ping", {}))
        return True

    async def mouse_move(self, x: int, y: int, *, relative: bool = False) -> bool:
        self._calls.append(("move", {"x": x, "y": y, "relative": relative}))
        if _cursor_mod._FAKE_DYNAMIC_STATE is None:
            initial = os.environ.get("CLAWTOUCH_FAKE_CURSOR", "960,540")
            try:
                sx, sy = initial.split(",", 1)
                _seed_fake_cursor(int(sx.strip()), int(sy.strip()))
            except (ValueError, AttributeError):
                _seed_fake_cursor(960, 540)
        _update_fake_cursor(x, y, relative=relative)
        return True

    async def mouse_click(self, button: str = "left", *, double: bool = False) -> bool:
        self._calls.append(("click", {"button": button, "double": double}))
        return True

    async def mouse_scroll(self, delta: int) -> bool:
        self._calls.append(("scroll", {"delta": delta}))
        return True

    async def mouse_button_down(self, button: str = "left") -> bool:
        self._calls.append(("button_down", {"button": button}))
        return True

    async def mouse_button_up(self, button: str = "left") -> bool:
        self._calls.append(("button_up", {"button": button}))
        return True

    async def type_text(self, text: str, *, chunk_size: int = 32, allow_control: bool = False) -> bool:
        self._calls.append(("type", {"text": text}))
        return True

    async def key_combo(self, modifiers: list[str], key: str) -> bool:
        self._calls.append(("key", {"modifiers": modifiers, "key": key}))
        return True

    async def key_press(self, key: str, modifiers: list[str] | None = None) -> bool:
        self._calls.append(("key_press", {"key": key, "modifiers": modifiers or []}))
        return True

    async def key_release(self, key: str = "", modifiers: list[str] | None = None) -> bool:
        self._calls.append(("key_release", {"key": key, "modifiers": modifiers or []}))
        return True

    async def release_all(self) -> bool:
        self._calls.append(("release_all", {}))
        return True

    async def device_info(self) -> dict:
        return {
            "port": self.port, "baudrate": self.baudrate,
            "connected": True, "mock": True, "calls": len(self._calls),
        }


class HidUnavailableError(RuntimeError):
    """Raised when no Pico HID hardware is reachable.

    Bubbles up through ``dispatch()`` to a JSON-RPC error visible to the
    AI client, which can then ask the user to free up the board (e.g.
    close ClawTouch desktop).
    """


class UnavailableBridge:
    """Stub bridge for startup-time HID unavailability.

    Unlike MockBridge (silent fake for `--mock` testing), every action on
    UnavailableBridge raises HidUnavailableError so the AI sees a clear
    reason and can ask the user to free up the hardware. Implements lazy
    retry: each action first tries to reconnect; on success it replaces
    ``server.bridge`` with a real SerialHidBridge and forwards the call.

    Cost of lazy retry: ~50ms per failed reconnect attempt. Worth it so
    the user does not need to restart the MCP client after freeing the
    hardware — next tool call auto-recovers.
    """

    def __init__(self, server: "ClawTouchMcpServer",
                 tried_ports: list[str], baudrate: int) -> None:
        self._server = server
        self._tried_ports = list(tried_ports)
        self._baudrate = baudrate
        self.is_connected = False
        self.port = "<unavailable>"
        self.baudrate = baudrate

    async def connect(self) -> None:  # pragma: no cover - trivial
        return None

    async def close(self) -> None:  # pragma: no cover - trivial
        return None

    async def _try_promote(self) -> bool:
        candidates: list[str] = []
        if self._server.config.port:
            candidates.append(self._server.config.port)
        for p in auto_detect_ports():
            if p not in candidates:
                candidates.append(p)
        for port in candidates:
            try:
                bridge = SerialHidBridge(port, baudrate=self._baudrate)
                await bridge.connect()
                self._server.bridge = bridge
                # Re-arm the idle-release watcher now that we hold a real
                # serial port again: _on_tool_call already ran
                # _ensure_idle_watch_started while the bridge was still
                # UnavailableBridge (so it no-op'd at the isinstance check).
                # Without this, a single tool call right after a lazy reconnect
                # followed by permanent silence would hold the COM port forever,
                # defeating HID coexistence with the ClawTouch desktop.
                self._server._ensure_idle_watch_started()
                logger.info(
                    "HID became available — promoted to real bridge on %s", port,
                )
                return True
            except Exception:
                continue
        self._tried_ports = candidates  # refresh for next failure message
        return False

    def _fail(self) -> None:
        raise HidUnavailableError(
            f"HID hardware is unavailable: tried port(s) {self._tried_ports}, "
            f"all busy or absent. Most likely another program is using the "
            f"Pico board exclusively (e.g. ClawTouch desktop client, Arduino "
            f"IDE, serial monitor). Please close any program that might be "
            f"using the same hardware, then retry this tool call."
        )

    async def _try_or_fail(self, method_name: str, *args, **kwargs):
        if await self._try_promote():
            return await getattr(self._server.bridge, method_name)(*args, **kwargs)
        self._fail()

    async def ping(self) -> bool:
        return await self._try_or_fail("ping")

    async def mouse_move(self, x: int, y: int, *, relative: bool = False) -> bool:
        return await self._try_or_fail("mouse_move", x, y, relative=relative)

    async def mouse_click(self, button: str = "left", *, double: bool = False) -> bool:
        return await self._try_or_fail("mouse_click", button, double=double)

    async def mouse_scroll(self, delta: int) -> bool:
        return await self._try_or_fail("mouse_scroll", delta)

    async def mouse_button_down(self, button: str = "left") -> bool:
        return await self._try_or_fail("mouse_button_down", button)

    async def mouse_button_up(self, button: str = "left") -> bool:
        return await self._try_or_fail("mouse_button_up", button)

    async def type_text(self, text: str, *, chunk_size: int = 32, allow_control: bool = False) -> bool:
        return await self._try_or_fail("type_text", text, chunk_size=chunk_size, allow_control=allow_control)

    async def key_combo(self, modifiers: list[str], key: str) -> bool:
        return await self._try_or_fail("key_combo", modifiers, key)

    async def key_press(self, key: str, modifiers: list[str] | None = None) -> bool:
        return await self._try_or_fail("key_press", key, modifiers)

    async def key_release(self, key: str = "", modifiers: list[str] | None = None) -> bool:
        return await self._try_or_fail("key_release", key, modifiers)

    async def release_all(self) -> bool:
        return await self._try_or_fail("release_all")

    async def device_info(self) -> dict:
        return {
            "port": self.port,
            "baudrate": self.baudrate,
            "connected": False,
            "available": False,
            "tried_ports": self._tried_ports,
            "reason": (
                "All candidate Pico ports busy/absent on startup; "
                "every action attempt also lazy-retries reconnect"
            ),
        }


def _decimate_rgb(rgb: bytes, raw_w: int, raw_h: int,
                  target_w: int, target_h: int):
    """Nearest-neighbour integer-stride downsample of an RGB byte buffer,
    pure Python (no Pillow / numpy). Used by the 'mss-png' screenshot backend
    to avoid returning a full physical-resolution Retina PNG (which would
    re-introduce the base64 buffer overflow the Pillow resize was added to
    prevent).

    Picks an integer factor f ≈ raw_w/target_w (2 on Retina) and keeps every
    f-th pixel/row. Per output row it does 3 C-speed strided slice-assigns,
    so a full Retina frame decimates in milliseconds. Returns
    ``(out_w, out_h, rgb_bytes)``; f<=1 returns the input unchanged. Fractional
    DPI (e.g. Windows 125%) rounds to the nearest integer factor — the caller
    reports the resulting scale honestly so click coords stay correct.
    """
    # Integer factor large enough that the decimated frame fits the target in
    # BOTH dimensions — ceil, NOT round. A 1.0–1.5x cap-only shrink (e.g. a
    # 5.94Mpx grab → 4Mpx cap target = 1.22x) rounds to f=1, which would skip
    # decimation and return a full-res multi-MB PNG, silently bypassing the 4M
    # output cap (the base64 overflow the cap exists to stop). ceil(a/b) for
    # positive ints == -(-a // b).
    if target_w and target_h:
        f = max(1, -(-raw_w // target_w), -(-raw_h // target_h))
    else:
        f = 1
    if f <= 1:
        return raw_w, raw_h, rgb
    out_w, out_h = raw_w // f, raw_h // f
    if out_w < 1 or out_h < 1:
        return raw_w, raw_h, rgb
    src_stride = raw_w * 3
    dst_stride = out_w * 3
    step = 3 * f
    out = bytearray(out_w * out_h * 3)
    for oy in range(out_h):
        s = oy * f * src_stride
        row = rgb[s:s + src_stride]
        o = oy * dst_stride
        out[o + 0:o + dst_stride:3] = row[0::step][:out_w]
        out[o + 1:o + dst_stride:3] = row[1::step][:out_w]
        out[o + 2:o + dst_stride:3] = row[2::step][:out_w]
    return out_w, out_h, bytes(out)


def _screenshot_pillow_note(err: BaseException, *, forced: bool) -> str:
    """Translate Pillow's cryptic dlopen / library-validation failure into a
    human-readable, actionable message (the original ImportError text is a
    wall of 'code signature ... different Team IDs' that reads as unfixable)."""
    s = str(err)
    libval = ("code signature" in s
              or "library validation" in s.lower()
              or "Team ID" in s
              or "not valid for use in process" in s)
    if libval:
        why = ("the host Python has hardened-runtime library validation "
               "enabled, which blocked Pillow's native _imaging extension "
               "(signed with a different Team ID)")
        fix = ("To use Pillow instead, run clawtouch-mcp from a Python "
               "without library validation, or grant the host Python the "
               "com.apple.security.cs.disable-library-validation entitlement")
    elif isinstance(err, ImportError):
        why = "Pillow is not installed"
        fix = ("install it for JPEG + Retina downscaling: "
               "pip install 'clawtouch-mcp[screenshot]'")
    else:
        why = f"Pillow could not be loaded ({type(err).__name__}: {err})"
        fix = "reinstall Pillow or pass --screenshot-backend mss-png"
    if forced:
        return f"--screenshot-backend=pillow was requested but {why}. {fix}."
    return (f"Screenshot is using the no-Pillow 'mss-png' backend because "
            f"{why}. Images are PNG at (down-sampled) logical resolution. "
            f"{fix}.")


# ═══════════════════ Server ═══════════════════

class ClawTouchMcpServer:
    def __init__(self, config: ServerConfig):
        self.config = config

        # Screenshot encode backend — resolved + cached lazily on the
        # first capture (a failing Pillow dlopen must not be retried
        # every screenshot).
        self._ss_backend: Optional[str] = None
        self._ss_image = None       # cached PIL.Image module when backend=pillow
        self._ss_note: Optional[str] = None

        # Windows DPI awareness MUST be set before any cursor / screen
        # query — both `_detect_screen` (here, when auto-detect runs)
        # AND `cursor.get_cursor_position` (later, on every absolute
        # hid.click) need physical-pixel semantics. Previously this
        # only ran inside `_detect_screen`; when the user passed an
        # explicit `--screen WxH`, DPI awareness was never enabled
        # and on a 125%-scaled host every absolute click was off by
        # ~25%. Set it unconditionally here.
        _ensure_windows_dpi_awareness()

        # Auto-detect screen size if not given. _screen_source surfaces in
        # device.info so the agent can tell explicit-vs-detected-vs-unset apart.
        if config.screen_w and config.screen_h:
            self._screen_source = "explicit"
        else:
            detected = _detect_screen()
            if detected is not None:
                config.screen_w, config.screen_h = detected
                self._screen_source = "detected"
                logger.info("auto-detected screen size: %dx%d",
                            detected[0], detected[1])
            else:
                self._screen_source = "unset"
                logger.warning(
                    "could not auto-detect screen size; coordinates will "
                    "not be clamped. Pass --screen WxH explicitly to enable."
                )
        # macOS Retina footgun: warn if an explicit --screen looks like
        # physical pixels rather than points (see method docstring).
        self._warn_if_retina_pixel_screen()
        self.bridge: Any = None
        self.rate = RateLimiter(config.ops_per_sec)
        self.tools: dict[str, Tool] = {}
        self._initialized = False
        # One-shot guard: flips True once we've warned about a self-interrupt
        # combo (cmd+q / alt+f4). Best-effort heads-up, fired at most once.
        self._warned_self_interrupt = False
        # release-on-idle: monotonic timestamp of last tool call. Init to now()
        # so server doesn't immediately release on startup before any call.
        self._last_used_at: float = time.monotonic()
        self._idle_task: Optional[asyncio.Task] = None
        self._stopping: bool = False
        # Counter of in-flight tool handlers. _idle_watch must not
        # close the serial port while a handler is mid-stream (a slow
        # type_text or a stalled hardware response can easily outlive
        # the idle_close_after deadline; cutting the bridge mid-command
        # leaves the firmware in an inconsistent state and surfaces as
        # a cryptic "ACK timeout" to the caller). The counter is
        # incremented before tool.handler runs and decremented after,
        # regardless of exception.
        self._inflight_handlers: int = 0
        # Set by the `shutdown` handler so `run_stdio` can exit cleanly.
        # `asyncio.Event` can be created without a running loop on 3.10+.
        self._stop_event: asyncio.Event = asyncio.Event()
        self._register_tools()

    def _warn_if_retina_pixel_screen(self) -> None:
        """Heuristic guard for the macOS point-vs-pixel ``--screen`` footgun.

        On macOS the OS cursor query (``cursor.get_cursor_position``) returns
        CoreGraphics *points*, not pixels — Retina displays scale points:pixels
        2:1. ``--screen`` / ``_clamp`` / the converge loop are pixel-agnostic:
        they trust whatever WxH you pass. If a user supplies a *physical-pixel*
        ``--screen`` on a Retina mac (e.g. ``2880x1800`` for a ``1440x900``-point
        display), every absolute click targets a coordinate the point-space
        cursor can never reach: ``_converge_to_target`` can't shrink the
        residual, exhausts ``MOVE_MAX_ITERS`` (=10), and returns ``ok=False``.
        A loud, graceful failure — but a baffling one without this hint.

        We detect the tell-tale ~2x-in-*both*-axes signature (Retina physical
        pixels) and warn. We deliberately do NOT warn on a rectangle that grows
        in only one axis — that's the legitimate multi-monitor bounding-box case
        (e.g. ``--screen 7680x1440`` to reach a side-by-side second monitor). And
        we warn rather than reject: an external display or unusual scale factor
        is the operator's call to make. No-op off macOS / when --screen wasn't
        given / when the logical size can't be detected (e.g. headless)."""
        if sys.platform != "darwin":
            return
        if self._screen_source != "explicit":
            return  # auto-detected size is already point-space-consistent
        w, h = self.config.screen_w, self.config.screen_h
        if not (w and h):
            return
        detected = _detect_screen()  # tkinter → LOGICAL points on macOS
        if not detected:
            return
        dw, dh = detected
        if dw <= 0 or dh <= 0:
            return
        sx, sy = w / dw, h / dh
        # ~2x (or 3x) in BOTH axes by the same factor ⇒ physical Retina pixels,
        # not a multi-monitor bounding box (which scales predominantly one axis).
        if sx >= 1.5 and sy >= 1.5 and abs(sx - sy) < 0.3:
            logger.warning(
                "--screen %dx%d looks like PHYSICAL Retina pixels, but this "
                "display reports %dx%d POINTS (~%.1fx scale). On macOS the OS "
                "cursor query returns points, so absolute clicks against a "
                "pixel-space --screen never converge (they return ok=false "
                "after exhausting the converge loop). Pass --screen in POINTS "
                "instead, e.g. --screen %dx%d.",
                w, h, dw, dh, sx, dw, dh,
            )

    # ── Lifecycle ──

    async def start(self) -> None:
        if self.config.mock:
            # Mock mode is a test/dev seam: honor the CLAWTOUCH_FAKE_CURSOR
            # env hook so the converge loop's first cursor query (before
            # MockBridge seeds its dynamic state) is deterministic. The hook
            # is OFF by default on real-hardware runs (stray-env safety).
            _cursor_mod._set_fake_cursor_allowed(True)
            self.bridge = MockBridge()
            logger.info("starting in MOCK mode — hardware is not touched")
            return
        # Collect candidate ports: explicit --port first, then auto-detected.
        # Try-list lets us survive coexistence with another process (e.g.
        # ClawTouch on the same machine) that already holds one of the boards.
        candidates: list[str] = []
        if self.config.port:
            candidates.append(self.config.port)
        for p in auto_detect_ports():
            if p not in candidates:
                candidates.append(p)
        if not candidates:
            logger.warning(
                "no Pico device detected; using UnavailableBridge "
                "(will lazy-retry on every tool call). "
                "Pass --port COMx or --mock to override."
            )
            self.bridge = UnavailableBridge(self, [], self.config.baudrate)
            return
        last_err: Exception | None = None
        for port in candidates:
            try:
                bridge = SerialHidBridge(port, baudrate=self.config.baudrate)
                await bridge.connect()
                self.bridge = bridge
                if len(candidates) > 1 and port != candidates[0]:
                    logger.info(
                        "connected to %s (first choice %s busy/unavailable)",
                        port, candidates[0],
                    )
                return
            except Exception as e:  # serial.SerialException, PermissionError, etc.
                last_err = e
                logger.warning(
                    "port %s unavailable: %s, trying next candidate", port, e,
                )
                continue
        logger.warning(
            "all %d candidate port(s) failed (last error: %s); using "
            "UnavailableBridge — every tool call will lazy-retry and surface "
            "a clear error to the AI client so the user can be told to free "
            "the hardware (e.g. close ClawTouch desktop)",
            len(candidates), last_err,
        )
        self.bridge = UnavailableBridge(self, candidates, self.config.baudrate)

    async def stop(self) -> None:
        self._stopping = True
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
            try:
                await self._idle_task
            except (asyncio.CancelledError, Exception):
                pass
            self._idle_task = None
        if self.bridge is not None:
            await self.bridge.close()

    # ── Release-on-idle ──
    # 30s 无 tools/call → close 串口 + 替换 self.bridge 为 UnavailableBridge,
    # 让其他进程 (如 ClawTouch desktop) 拿到同一块板. 下次 tools/call 由
    # UnavailableBridge._try_promote 自动 lazy reconnect 回 SerialHidBridge.
    # Lazy 启动: 第一次 tools/call 时才启动 idle watch (没人用就不计时).

    def _ensure_idle_watch_started(self) -> None:
        if self._stopping:
            return
        if self.config.idle_close_after <= 0:
            return  # feature disabled
        if not isinstance(self.bridge, SerialHidBridge):
            return  # MockBridge / UnavailableBridge 不需要 release
        if self._idle_task is not None and not self._idle_task.done():
            return  # 已在跑
        try:
            self._idle_task = asyncio.create_task(self._idle_watch())
        except RuntimeError:
            # 没有运行中的 event loop (e.g. unit test 没起 loop); 静默
            pass

    async def _idle_watch(self) -> None:
        """周期检查 idle 阈值. 触发 release 后退出, 下次 tool call 重新启动.

        Unhandled exceptions inside this task used to be swallowed silently
        by ``asyncio.Task``'s exception machinery: the task would die,
        ``_idle_task.done()`` would return True, and the next tool call's
        ``_ensure_idle_watch_started()`` would refuse to restart it because
        the slot was still occupied by a finished task. Net effect: the
        serial port would be held forever, never released back to the bus.
        We now log + reset the slot so the next tool call restarts the
        watcher and the release-on-idle invariant is restored.
        """
        if self.config.idle_check_interval > 0:
            check_interval = self.config.idle_check_interval
        else:
            check_interval = max(1.0, min(5.0, self.config.idle_close_after / 6.0))
        try:
            while not self._stopping:
                await asyncio.sleep(check_interval)
                if not isinstance(self.bridge, SerialHidBridge):
                    return  # bridge 已不是真 serial (e.g. 被 stop), 退出
                if time.monotonic() - self._last_used_at >= self.config.idle_close_after:
                    # In-flight protection: a slow handler (4096-char
                    # type_text, stalled hardware) can outlive the
                    # deadline. Releasing the bridge mid-command leaves
                    # the firmware in a bad state. Defer the release
                    # until the handler returns; the loop will re-check
                    # on the next tick. _on_tool_call refreshes
                    # _last_used_at on the way out so we won't fire
                    # right after either.
                    if self._inflight_handlers > 0:
                        continue
                    await self._idle_release_now()
                    return  # release 后 bridge 是 UnavailableBridge, 退出
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "_idle_watch crashed unexpectedly — resetting slot so the next "
                "tool call can restart the watcher (HID stays held until then)"
            )
            self._idle_task = None
            return

    async def _idle_release_now(self) -> None:
        """强制立即 release HID (不管 idle 计时), 把 SerialHidBridge 替换为
        UnavailableBridge. 已是非 SerialHidBridge 时 no-op."""
        if not isinstance(self.bridge, SerialHidBridge):
            return
        port = getattr(self.bridge, "port", "<unknown>")
        try:
            await self.bridge.close()
        except Exception as e:
            logger.warning("idle close failed: %s", e)
        self.bridge = UnavailableBridge(self, [port], self.config.baudrate)
        logger.info(
            "HID idle %.0fs, released COM %s — next tool call will lazy "
            "reconnect via UnavailableBridge._try_promote",
            self.config.idle_close_after, port,
        )

    # ── Safety helpers ──

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        if self.config.screen_w and self.config.screen_h:
            x = max(0, min(int(x), self.config.screen_w - 1))
            y = max(0, min(int(y), self.config.screen_h - 1))
        return int(x), int(y)

    # ── Tool registry ──

    def _register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def _register_tools(self) -> None:
        self._register(Tool(
            name="hid.click",
            description=(
                HID_PREFIX +
                "Click mouse. Default semantics: (x, y) is an ABSOLUTE "
                "screen coordinate — the server queries the OS for the "
                "current cursor position (Win32 GetCursorPos / macOS "
                "CGEventGetLocation / Linux/X11 XQueryPointer via ctypes) "
                "and emits a relative move so the firmware (which is a "
                "USB Boot Mouse and only supports relative deltas) lands "
                "at the target. Pass relative=true to skip the OS query "
                "and send (x, y) directly as a pixel delta. On Wayland "
                "and on hosts where the OS cursor query fails, absolute "
                "mode returns an error and the caller must use "
                "relative=true.\n\n"
                "Absolute mode runs a closed-loop converge (query → "
                "delta → settle, up to 10 iterations, ≤5 px tolerance) "
                "to absorb OS pointer-ballistics non-linearity (macOS "
                "scales single HID deltas ~110% in the low-speed "
                "segment, so a fire-and-forget move overshoots by "
                "10-90 px). The returned `x`/`y` are the actual "
                "landing coordinates; `target_x`/`target_y` echo the "
                "request; `converged: true` means residual ≤5 px. "
                "The click only fires after the move succeeds — i.e. the "
                "cursor is confirmed within ≤5 px of target. If convergence "
                "fails (or the OS cursor query is unavailable), NO click is "
                "sent and the move result is returned unchanged (`ok: false` "
                "plus `converged`/`residual_x`/`residual_y`/`hint`); inspect "
                "those and retry.\n\n"
                "Optional `move_ms` switches to glide mode: the move "
                "is broken into ~10 ms HID reports over N ms (linear "
                "interpolation, then a closed-loop converge pass to "
                "clean up the final landing). Default 0 = snap mode."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                    "double": {"type": "boolean", "default": False},
                    "relative": {"type": "boolean", "default": False,
                                 "description": "If true, x/y are pixel deltas; absolute mode is skipped."},
                    "move_ms": {
                        "type": "integer", "default": 0,
                        "minimum": 0, "maximum": MAX_MOVE_MS,
                        "description": (
                            "Glide mode: break the move into ~10 ms "
                            "HID reports over N ms (linear interp + "
                            "post-slide converge). 0 = snap mode "
                            "(default, instant move)."
                        ),
                    },
                },
                "required": ["x", "y"],
            },
            handler=self._tool_click,
        ))
        self._register(Tool(
            name="hid.move",
            description=(
                HID_PREFIX +
                "Move mouse. Default semantics: (x, y) is an ABSOLUTE "
                "screen coordinate (see hid.click for how absolute mode "
                "works under the hood, including the closed-loop "
                "convergence that absorbs OS pointer-ballistics). Pass "
                "relative=true to send (x, y) as a pixel delta directly. "
                "On hosts where the OS cursor query is unavailable, "
                "absolute mode returns an error.\n\n"
                "Returns `x`/`y` = actual landing coordinates, "
                "`target_x`/`target_y` = original request, `converged` "
                "/ `iters` for the absolute path. Optional `move_ms` "
                "switches snap mode (default) → glide mode; see "
                "hid.click for the trade-off."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "relative": {"type": "boolean", "default": False},
                    "move_ms": {
                        "type": "integer", "default": 0,
                        "minimum": 0, "maximum": MAX_MOVE_MS,
                        "description": (
                            "Glide mode: break the move into ~10 ms "
                            "HID reports over N ms (linear interp + "
                            "post-slide converge). 0 = snap mode "
                            "(default, instant move)."
                        ),
                    },
                },
                "required": ["x", "y"],
            },
            handler=self._tool_move,
        ))
        self._register(Tool(
            name="hid.hover",
            description=(
                HID_PREFIX +
                "Move mouse to (x,y) then idle for duration_ms (no click). "
                "`duration_ms` is the IDLE time AFTER reaching the target; "
                "`move_ms` (optional) is the time spent on the move ITSELF "
                "(glide mode). Default move_ms=0 = snap mode (instant "
                "move), then idles. Absolute mode runs the same "
                "closed-loop converge as hid.click — see that tool's "
                "description for landing / convergence semantics."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "duration_ms": {"type": "integer", "default": 500, "minimum": 0, "maximum": 10000,
                                    "description": "Idle time AFTER reaching target."},
                    "move_ms": {
                        "type": "integer", "default": 0,
                        "minimum": 0, "maximum": MAX_MOVE_MS,
                        "description": (
                            "Glide mode for the move itself: break "
                            "into ~10 ms HID reports over N ms (linear "
                            "interp + post-slide converge). 0 = snap "
                            "mode (default, instant move)."
                        ),
                    },
                },
                "required": ["x", "y"],
            },
            handler=self._tool_hover,
        ))
        self._register(Tool(
            name="hid.type",
            description=HID_PREFIX + "Type a string as if on a physical keyboard (US layout).",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=self._tool_type,
        ))
        self._register(Tool(
            name="hid.scroll",
            description=HID_PREFIX + "Scroll the mouse wheel. Positive=up, negative=down.",
            input_schema={
                "type": "object",
                "properties": {"delta": {"type": "integer"}},
                "required": ["delta"],
            },
            handler=self._tool_scroll,
        ))
        self._register(Tool(
            name="hid.key",
            description=HID_PREFIX + "Press a key or keyboard shortcut.",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "Named key (enter/tab/f1…), a single character, "
                            "or shortcut shorthand like 'ctrl+c' or 'ctrl+alt+l' "
                            "— modifiers in the prefix are split out and combined "
                            "with the modifiers array."
                        ),
                    },
                    "modifiers": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["ctrl", "shift", "alt", "gui", "win", "cmd"]},
                        "default": [],
                        "description": (
                            "Explicit modifier list. Combined with any modifiers "
                            "parsed from the key shorthand. Optional when the key "
                            "field already encodes the modifiers (e.g. 'ctrl+c')."
                        ),
                    },
                },
                "required": ["key"],
            },
            handler=self._tool_key,
        ))
        self._register(Tool(
            name="hid.release_all",
            description=HID_PREFIX + "Release every held key / mouse button (panic stop).",
            input_schema={"type": "object", "properties": {}},
            handler=self._tool_release_all,
        ))
        # ── v1.1 additions: independent press/release primitives + composed gestures ──
        self._register(Tool(
            name="hid.mouse_button_down",
            description=(
                HID_PREFIX +
                "Press a mouse button WITHOUT releasing it. Pair with "
                "hid.mouse_button_up (and hid.move in between) to compose "
                "a drag, or use hid.drag for a one-call wrapper. Matches "
                "Anthropic Computer Use's left_mouse_down action."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                },
            },
            handler=self._tool_mouse_button_down,
        ))
        self._register(Tool(
            name="hid.mouse_button_up",
            description=(
                HID_PREFIX +
                "Release a previously-pressed mouse button. Idempotent — "
                "releasing a non-held button is a no-op (no error). "
                "Matches Anthropic Computer Use's left_mouse_up action."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                },
            },
            handler=self._tool_mouse_button_up,
        ))
        self._register(Tool(
            name="hid.drag",
            description=(
                HID_PREFIX +
                "Drag from (from_x, from_y) to (to_x, to_y) while holding "
                "the named button. Internally: absolute move to source → "
                "mouse_button_down → glided absolute move to destination → "
                "mouse_button_up. Matches Anthropic Computer Use's "
                "left_click_drag action. Useful for design / spreadsheet / "
                "file-manager workflows where 'press → drag → release' is "
                "the atomic UI gesture."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_x": {"type": "integer"},
                    "from_y": {"type": "integer"},
                    "to_x": {"type": "integer"},
                    "to_y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                    "move_ms": {
                        "type": "integer", "default": 300, "minimum": 0, "maximum": MAX_MOVE_MS,
                        "description": "Duration of the held-button move from source to destination.",
                    },
                    "relative": {
                        "type": "boolean", "default": False,
                        "description": "If true, from_x/y and to_x/y are pixel deltas, not absolute coords.",
                    },
                },
                "required": ["from_x", "from_y", "to_x", "to_y"],
            },
            handler=self._tool_drag,
        ))
        self._register(Tool(
            name="hid.key_press",
            description=(
                HID_PREFIX +
                "Press a key (or shortcut) WITHOUT releasing. Pair with "
                "hid.key_release. Useful for 'hold shift while clicking N "
                "times' multi-select patterns: hid.key_press('shift') → "
                "several hid.click → hid.key_release('shift'). For a "
                "fixed-duration hold, prefer hid.hold_key."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "modifiers": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["ctrl", "shift", "alt", "gui", "win", "cmd"]},
                        "default": [],
                    },
                },
                "required": ["key"],
            },
            handler=self._tool_key_press,
        ))
        self._register(Tool(
            name="hid.key_release",
            description=(
                HID_PREFIX +
                "Release a previously-pressed key (or shortcut). Idempotent. "
                "Pass no arguments to release ALL held keys and mouse "
                "buttons (panic stop, same as hid.release_all)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "default": ""},
                    "modifiers": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["ctrl", "shift", "alt", "gui", "win", "cmd"]},
                        "default": [],
                    },
                },
            },
            handler=self._tool_key_release,
        ))
        self._register(Tool(
            name="hid.hold_key",
            description=(
                HID_PREFIX +
                "Press a key, wait duration_ms, then release. Matches "
                "Anthropic Computer Use's hold_key action. Useful for "
                "scenarios where a single tap is too short — e.g. holding "
                "an arrow key to scroll a long list, or holding Space to "
                "pan in a design app."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "duration_ms": {"type": "integer", "default": 500, "minimum": 1, "maximum": 10000},
                    "modifiers": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["ctrl", "shift", "alt", "gui", "win", "cmd"]},
                        "default": [],
                    },
                },
                "required": ["key"],
            },
            handler=self._tool_hold_key,
        ))
        self._register(Tool(
            name="hid.batch",
            description=(
                HID_PREFIX +
                "Run a SHORT, PRE-PLANNED sequence of HID actions (max "
                f"{MAX_BATCH_OPS}) in ONE call, in strict order. This is a "
                "transport convenience for an action list you ALREADY know "
                "— e.g. clicking several fixed coordinates a solver has "
                "computed — collapsing N tool round-trips into one. It is "
                "NOT an orchestration / control-flow layer: no branching, "
                "no reading a result mid-sequence, no looping. For "
                "'act → observe → decide → act' you still issue separate "
                "calls (an action that depends on an earlier action's "
                "outcome cannot be pre-planned into a batch).\n\n"
                "Each op is {type, ...params, delay_ms?}. Types:\n"
                "  • click / move — (x, y, relative, button, double, "
                "move_ms); identical absolute closed-loop converge and "
                "ACK semantics to hid.click / hid.move.\n"
                "  • button_down / button_up — (button).\n"
                "  • key — (key, modifiers); same 'ctrl+c' shorthand as "
                "hid.key.\n"
                "  • type — (text).\n"
                "  • scroll — (delta).\n"
                "`delay_ms` pauses AFTER that op "
                f"(0–{MAX_BATCH_DELAY_MS} ms). Omit it and click/button ops "
                f"get a small default gap (~{DEFAULT_CLICK_SETTLE_MS} ms) so "
                "the OS doesn't merge or drop back-to-back clicks; non-click "
                "ops default to 0. Set delay_ms explicitly (including 0) to "
                "override.\n\n"
                "Execution: ops run strictly sequentially. With "
                "stop_on_error=true (default) the run halts at the first "
                "op that fails; if any button/key was pressed before the "
                "stop, release_all fires so nothing stays held. Returns "
                "{ok (= every op ok), count, failed_index, stopped_early, "
                "released_all, results:[per-op dicts carrying the same "
                "fields the standalone tool returns — e.g. converged / "
                "clicked / chars]}. Held state is NOT auto-released on "
                "clean completion, so a batch may intentionally leave a "
                "button/key down for a follow-up call.\n\n"
                f"Capped at {MAX_BATCH_OPS} ops: this drives real input "
                "and a batch cannot be interrupted mid-run (stdio is "
                "serial), so a large blind burst is refused at the "
                "boundary."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ops": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": MAX_BATCH_OPS,
                        "description": (
                            f"Up to {MAX_BATCH_OPS} HID actions, executed "
                            "in array order."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["click", "move", "button_down",
                                             "button_up", "key", "type",
                                             "scroll"],
                                },
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "button": {"type": "string",
                                           "enum": ["left", "right", "middle"],
                                           "default": "left"},
                                "double": {"type": "boolean", "default": False},
                                "relative": {"type": "boolean", "default": False},
                                "move_ms": {"type": "integer", "default": 0,
                                            "minimum": 0, "maximum": MAX_MOVE_MS},
                                "key": {"type": "string"},
                                "modifiers": {
                                    "type": "array",
                                    "items": {"type": "string",
                                              "enum": ["ctrl", "shift", "alt",
                                                       "gui", "win", "cmd"]},
                                    "default": [],
                                },
                                "text": {"type": "string"},
                                "delta": {"type": "integer"},
                                "delay_ms": {"type": "integer",
                                             "minimum": 0,
                                             "maximum": MAX_BATCH_DELAY_MS,
                                             "description": (
                                                 "Pause AFTER this op (ms). "
                                                 "Omit for a smart default: "
                                                 f"~{DEFAULT_CLICK_SETTLE_MS} ms "
                                                 "after click/button ops (so "
                                                 "back-to-back clicks aren't "
                                                 "merged/dropped by the OS), 0 "
                                                 "otherwise. Set explicitly "
                                                 "(including 0) to override."
                                             )},
                            },
                            "required": ["type"],
                        },
                    },
                    "stop_on_error": {
                        "type": "boolean", "default": True,
                        "description": (
                            "Halt at the first failing op (default). "
                            "false = run every op, recording failures."
                        ),
                    },
                },
                "required": ["ops"],
            },
            handler=self._tool_batch,
        ))
        self._register(Tool(
            name="device.list",
            description="List candidate Pico serial ports.",
            input_schema={"type": "object", "properties": {}},
            handler=self._tool_device_list,
        ))
        self._register(Tool(
            name="device.info",
            description="Active bridge's connection + sequence info.",
            input_schema={"type": "object", "properties": {}},
            handler=self._tool_device_info,
        ))
        if self.config.allow_screenshot:
            self._register(Tool(
                name="hid.screenshot",
                description=(
                    HID_PREFIX +
                    "Take a screenshot (requires --allow-screenshot + mss; "
                    "Pillow optional via the '[screenshot]' extras for JPEG "
                    "+ higher-quality Retina downscaling). Returned as MCP "
                    "image content (vision-token path) so Retina captures "
                    "don't overflow the tool-result text buffer. Default "
                    "format is JPEG q80 — pass format='png' for pixel-perfect "
                    "OCR-style work. Captured at logical-point space on "
                    "high-DPI displays (auto-downsampled when the physical "
                    "buffer is noticeably larger than --screen WxH), so "
                    "hid.click coordinates from the screenshot are 1:1 with "
                    "click_point space; scale_x / scale_y ~1.0 on Retina. "
                    "ALWAYS divide screenshot coords by scale_x / scale_y "
                    "before clicking. The metadata 'backend' field is "
                    "'pillow' or 'mss-png' — the latter is a no-native-"
                    "extension fallback auto-selected under hardened-runtime "
                    "library-validation hosts (PNG only). Capped at 4M "
                    "output pixels (see raw_size for the original grab)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "region": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 4, "maxItems": 4,
                            "description": "[x1,y1,x2,y2] in screenshot pixel coordinates",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["jpeg", "png"],
                            "description": (
                                "Encoding format. Default 'jpeg' (q80, "
                                "smaller payload). 'png' for lossless."
                            ),
                        },
                    },
                },
                handler=self._tool_screenshot,
            ))

    # ── Tool handlers ──

    def _absolute_to_relative(self, target_x: int, target_y: int) -> tuple[int, int] | None:
        """Translate an absolute target coordinate into a relative
        delta the firmware can actually execute, by querying the OS
        cursor position. Returns ``None`` when OS cursor tracking is
        unavailable on the current platform — callers should surface
        a clear error and ask the agent to use ``relative=True``.

        See `cursor.py` for the per-OS implementation; the firmware
        itself is a USB Boot Mouse and can only emit relative deltas.
        """
        current = get_cursor_position()
        if current is None:
            return None
        cur_x, cur_y = current
        return (target_x - cur_x, target_y - cur_y)

    def _cursor_unavailable_error(self, target_x: int, target_y: int) -> dict[str, Any]:
        return {
            "error": (
                "Absolute coordinates require OS-level cursor "
                "tracking, which is unavailable on this host. "
                + availability_hint()
                + " As a workaround, call hid.move / hid.click "
                "with `relative=true` and supply pixel deltas."
            ),
            "x": target_x, "y": target_y,
        }

    @staticmethod
    def _move_failed(result: dict) -> bool:
        """True when a move-helper result represents a failure that must
        stop the dependent action (click / drag / keypress).

        A move "failed" if it carries an ``error`` (OS cursor query
        unavailable) or an explicit ``ok is False`` — the latter is set
        by ``_converge_to_target`` when the closed-loop give up (cursor
        never reached the target within tolerance) and by the stepped
        relative move when a sub-report was not ACKed. Composed tools use
        this to avoid clicking / dragging / pressing at a location we
        never confirmed reaching, and to avoid reporting success for a
        gesture whose positioning step silently failed.
        """
        return "error" in result or result.get("ok") is False

    @staticmethod
    def _looks_device_nonresponsive(result: dict) -> bool:
        """True when a per-op result indicates the HID device has stopped
        responding (unplugged / firmware hung) rather than a recoverable
        per-op error. The move helpers set ``device_nonresponsive`` after a
        run of consecutive un-ACKed reports; single bridge calls (scroll /
        key / type / button) surface the same condition via the bridge's
        ACK-timeout / not-connected diagnostic. hid.batch uses this to stop a
        continue-on-error run the instant the device dies, instead of
        grinding every remaining op through its full ACK timeout — a dead
        device can't recover mid-batch. A recoverable error (bad arg,
        firmware ERROR, seq mismatch, no convergence) returns False so
        ``stop_on_error`` semantics are unchanged for those."""
        if result.get("device_nonresponsive"):
            return True
        diag = str(result.get("bridge_diagnostic") or "").lower()
        return "ack timeout" in diag or "not connected" in diag

    async def _converge_to_target(
        self, target_x: int, target_y: int, *, max_iters: int,
    ) -> dict[str, Any]:
        """Closed-loop settle to (target_x, target_y). Query OS cursor,
        emit residual delta, sleep one HID cycle, repeat until residual
        ≤ MOVE_TOLERANCE or max_iters exhausted.

        Returns:
            {"ok": True,  "x": actual, "y": actual, "target_x", "target_y",
             "iters": int, "converged": True,
             "move_acked": bool}                 on success / short-circuit;
            {"ok": False, "x": actual, "y": actual, "target_x", "target_y",
             "residual_x", "residual_y", "iters": max_iters,
             "converged": False, "move_acked": bool,
             "hint": ...}                        when residual stays > tol;
            {"error": ...}                       when OS cursor query fails.
        """
        landed: tuple[int, int] | None = None
        all_acked = True
        moves_made = 0
        consecutive_timeouts = 0
        nonresponsive = False
        for i in range(max_iters):
            cur = get_cursor_position()
            if cur is None:
                return self._cursor_unavailable_error(target_x, target_y)
            dx = target_x - cur[0]
            dy = target_y - cur[1]
            if abs(dx) <= MOVE_TOLERANCE and abs(dy) <= MOVE_TOLERANCE:
                return {
                    "ok": True,
                    "x": cur[0], "y": cur[1],
                    "target_x": target_x, "target_y": target_y,
                    "iters": i,
                    "converged": True,
                    # AND of every in-loop move's ACK so far. The cursor is
                    # ground truth (we converged), so this is purely a
                    # diagnostic: True normally; False means a move landed
                    # the cursor on target despite the firmware not ACKing
                    # it — reported consistently with the non-converged path
                    # rather than omitted on success.
                    "move_acked": all_acked,
                }
            move_acked = await self.bridge.mouse_move(dx, dy, relative=True)
            all_acked = all_acked and bool(move_acked)
            moves_made += 1
            landed = (cur[0] + dx, cur[1] + dy)
            # Death-spiral guard: a dead/unplugged device never ACKs and the
            # cursor never moves, so the residual can't shrink — without this
            # the loop would spend max_iters × per-ACK timeout (~10 s) before
            # giving up. Bail after a short run of consecutive non-ACKs (a
            # single transient drop resets the counter and rides through).
            if move_acked:
                consecutive_timeouts = 0
            else:
                consecutive_timeouts += 1
                if consecutive_timeouts >= MAX_CONSECUTIVE_MOVE_TIMEOUTS:
                    nonresponsive = True
                    break
            if i < max_iters - 1:
                await asyncio.sleep(MOVE_SETTLE_MS / 1000.0)
        actual = get_cursor_position() or landed or (target_x, target_y)
        result: dict[str, Any] = {
            "ok": False,
            "x": actual[0], "y": actual[1],
            "target_x": target_x, "target_y": target_y,
            "residual_x": target_x - actual[0],
            "residual_y": target_y - actual[1],
            "iters": moves_made,
            "converged": False,
            # False ⇒ at least one in-loop mouse_move was not ACKed by the
            # firmware; distinguishes "bridge dropped commands" from
            # "commands landed but the cursor drifted" for the agent.
            "move_acked": all_acked,
            "hint": (
                f"cursor did not converge within tolerance after {moves_made} "
                "iterations. The actual (x, y) is usually only a few px from "
                "target and may be close enough to act on — inspect the "
                "residual. If the residual is large, a competing input device "
                "(trackpad / physical mouse) moving the cursor mid-move, "
                "extreme pointer-acceleration settings, or a UI dead zone is "
                "the likely cause; decide whether to retry."
            ),
        }
        if nonresponsive:
            # Distinguish "device gone" from "cursor drifted": the former is
            # not worth retrying without a reconnect, and hid.batch uses this
            # flag to stop a continue-on-error run instead of grinding on.
            result["device_nonresponsive"] = True
            result["hint"] = (
                f"aborted after {consecutive_timeouts} consecutive un-ACKed "
                "mouse reports — the device is not responding (unplugged / "
                "firmware hung). The cursor was not moved onto target; "
                "reconnect and retry."
            )
        return result

    async def _move_to_absolute(self, target_x: int, target_y: int) -> dict[str, Any]:
        """Snap-to absolute move (default, ``move_ms=0``). Clamps to
        screen, then closed-loop converges via _converge_to_target.

        If ``"error"`` is in the returned dict the caller should
        propagate it as the tool error without continuing."""
        target_x, target_y = self._clamp(target_x, target_y)
        return await self._converge_to_target(
            target_x, target_y, max_iters=MOVE_MAX_ITERS,
        )

    # ── Path stepping (for visible cursor motion in demos) ────────
    #
    # ``hid.click`` / ``hid.move`` / ``hid.hover`` accept an optional
    # ``move_ms`` argument. When > 0, the helpers below break the
    # move into ~10 ms HID reports so the OS cursor visibly slides
    # to the target instead of teleporting in a single frame. This
    # is a *visual smoothness* convenience equivalent to PyAutoGUI's
    # ``duration=`` parameter — purely linear interpolation, no
    # curves / no tremor / no dwell variance, so it can't be
    # confused with anything richer that lives on top.

    def _plan_step_count(self, move_ms: int) -> int:
        """~10 ms per step, min 4 (so a 40 ms move still has motion),
        max 100 (so a stupid ``move_ms`` can't pile up reports)."""
        return max(4, min(100, move_ms // 10))

    async def _stepped_move_to_absolute(
        self, target_x: int, target_y: int, move_ms: int,
    ) -> dict[str, Any]:
        """Glide mode: chunk the move into N ~10 ms HID reports over
        ``move_ms`` total so the cursor visibly slides instead of
        teleporting. macOS pointer ballistics applies to every report
        (including the last micro-step), so the slide alone lands
        with the same 10-90 px error as snap mode — after the slide
        we run a short closed-loop converge to pull the cursor onto
        the target.

        Caller signals intent via the ``move_ms`` tool argument;
        ``move_ms == 0`` (default) goes through ``_move_to_absolute``
        instead (snap mode)."""
        target_x, target_y = self._clamp(target_x, target_y)
        cur = get_cursor_position()
        if cur is None:
            return self._cursor_unavailable_error(target_x, target_y)
        total_dx = target_x - cur[0]
        total_dy = target_y - cur[1]
        steps = self._plan_step_count(move_ms)
        step_ms = move_ms / steps
        # We pre-compute the full delta and chunk against it (not
        # against intermediate cursor queries) because the OS cursor
        # position lags behind the HID stream — each report we emit
        # is still being processed when the next step plans. Trusting
        # ``accumulated_*`` here is correct precisely because the
        # firmware is a Boot Mouse: every delta we send lands.
        accumulated_dx = 0
        accumulated_dy = 0
        slide_acked = True
        consecutive_timeouts = 0
        slide_aborted = False
        for i in range(1, steps + 1):
            t = i / steps
            target_dx = round(total_dx * t)
            target_dy = round(total_dy * t)
            step_dx = target_dx - accumulated_dx
            step_dy = target_dy - accumulated_dy
            if step_dx or step_dy:
                step_acked = await self.bridge.mouse_move(step_dx, step_dy, relative=True)
                slide_acked = slide_acked and bool(step_acked)
                # Death-spiral guard: bail out of the glide once the device
                # stops ACKing — otherwise a dead device drags the slide
                # through all `steps` reports at the full per-ACK timeout each.
                if step_acked:
                    consecutive_timeouts = 0
                else:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= MAX_CONSECUTIVE_MOVE_TIMEOUTS:
                        slide_aborted = True
                        break
            accumulated_dx = target_dx
            accumulated_dy = target_dy
            if i < steps:
                await asyncio.sleep(step_ms / 1000)
        if slide_aborted:
            # Device stopped responding mid-glide. Skip the converge stage
            # (it would only re-spend the same ACK-timeout dead-air) and fail
            # fast with ok:False so the caller's dependent action (click) does
            # not fire at a location we never reached. _move_failed() keys off
            # ``ok is False``; device_nonresponsive lets hid.batch stop.
            return {
                "ok": False,
                "stepped": True,
                "steps": steps,
                "move_ms": move_ms,
                "slide_acked": False,
                "converged": False,
                "device_nonresponsive": True,
                "target_x": target_x, "target_y": target_y,
                "hint": (
                    "aborted glide: the device stopped ACKing mouse reports "
                    "(unplugged / firmware hung). No dependent action fired; "
                    "reconnect and retry."
                ),
            }
        # Slide done — closed-loop converge with the SAME budget as snap
        # mode (full MOVE_MAX_ITERS, not one fewer). The slide's final
        # micro-step is itself ballistics-amplified, so it lands the
        # cursor tens of px off — the same order as a cold-start move —
        # and earns no smaller budget. (Regression: an earlier
        # ``MOVE_MAX_ITERS - 1`` left glide landings 4-7 px off that the
        # click gate then refused; real-hardware mac dogfood 2026-06-04.)
        # ``ok`` comes from the converge stage (cursor-verified ground
        # truth): if the slide dropped a report but converge still pulled
        # the cursor onto target, the move genuinely succeeded — we record
        # ``slide_acked`` for diagnostics without masking that success.
        result = await self._converge_to_target(
            target_x, target_y, max_iters=MOVE_MAX_ITERS,
        )
        if "error" in result:
            return result
        result["stepped"] = True
        result["steps"] = steps
        result["move_ms"] = move_ms
        result["slide_acked"] = slide_acked
        return result

    async def _stepped_relative_move(
        self, dx: int, dy: int, move_ms: int,
    ) -> dict[str, Any]:
        """Same path stepping for ``relative=true`` callers — chunks
        the agent-supplied (dx, dy) into ~10 ms HID reports.

        Relative moves have no OS-cursor feedback loop (the agent owns the
        deltas), so the firmware ACK is the only success signal we have:
        ``ok`` is the AND of every emitted report's ACK. A dropped report
        means the gesture did not fully land, and callers must not treat
        it as success."""
        steps = self._plan_step_count(move_ms)
        step_ms = move_ms / steps
        accumulated_dx = 0
        accumulated_dy = 0
        all_acked = True
        consecutive_timeouts = 0
        nonresponsive = False
        for i in range(1, steps + 1):
            t = i / steps
            target_dx = round(dx * t)
            target_dy = round(dy * t)
            step_dx = target_dx - accumulated_dx
            step_dy = target_dy - accumulated_dy
            if step_dx or step_dy:
                step_acked = await self.bridge.mouse_move(step_dx, step_dy, relative=True)
                all_acked = all_acked and bool(step_acked)
                # Death-spiral guard (see _stepped_move_to_absolute): stop
                # chunking once the device stops ACKing rather than firing all
                # `steps` reports at the full per-ACK timeout.
                if step_acked:
                    consecutive_timeouts = 0
                else:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= MAX_CONSECUTIVE_MOVE_TIMEOUTS:
                        nonresponsive = True
                        break
            accumulated_dx = target_dx
            accumulated_dy = target_dy
            if i < steps:
                await asyncio.sleep(step_ms / 1000)
        result: dict[str, Any] = {
            "ok": all_acked,
            "dx": dx, "dy": dy,
            "stepped": True, "steps": steps, "move_ms": move_ms,
            "relative": True,
        }
        if nonresponsive:
            result["device_nonresponsive"] = True
        return result

    async def _tool_click(self, **kw) -> dict:
        self.rate.check()
        relative = bool(kw.get("relative", False))
        move_ms = max(0, min(MAX_MOVE_MS, int(kw.get("move_ms") or 0)))
        if relative:
            # Agent wants raw relative move — skip the cursor query. The
            # firmware ACK is the only signal that the move landed.
            dx, dy = int(kw["x"]), int(kw["y"])
            if move_ms > 0:
                result = await self._stepped_relative_move(dx, dy, move_ms)
            else:
                move_ok = await self.bridge.mouse_move(dx, dy, relative=True)
                result: dict[str, Any] = {
                    "ok": move_ok, "dx": dx, "dy": dy, "relative": True,
                }
        else:
            if move_ms > 0:
                result = await self._stepped_move_to_absolute(
                    kw["x"], kw["y"], move_ms,
                )
            else:
                result = await self._move_to_absolute(kw["x"], kw["y"])
        # Positioning failed (cursor unavailable / no convergence / a move
        # report was not ACKed). Do NOT click — we'd be clicking somewhere
        # we never confirmed reaching. Surface the move failure unchanged
        # so the agent sees the reason (and so a click ACK can't mask it).
        if self._move_failed(result):
            return result
        click_ok = await self.bridge.mouse_click(
            button=kw.get("button", "left"),
            double=bool(kw.get("double", False)),
        )
        result["ok"] = click_ok
        result["clicked"] = click_ok
        return result

    async def _tool_move(self, **kw) -> dict:
        self.rate.check()
        relative = bool(kw.get("relative", False))
        move_ms = max(0, min(MAX_MOVE_MS, int(kw.get("move_ms") or 0)))
        if relative:
            x, y = int(kw["x"]), int(kw["y"])
            if move_ms > 0:
                # Keep the move's real ``ok`` (AND of every report's ACK) —
                # don't override it with an unconditional True.
                result = await self._stepped_relative_move(x, y, move_ms)
                result.update({"x": x, "y": y})
                return result
            ok = await self.bridge.mouse_move(x, y, relative=True)
            return {"ok": ok, "x": x, "y": y, "relative": True}
        if move_ms > 0:
            moved = await self._stepped_move_to_absolute(
                kw["x"], kw["y"], move_ms,
            )
        else:
            moved = await self._move_to_absolute(kw["x"], kw["y"])
        if "error" in moved:
            return moved
        moved["relative"] = False
        return moved

    async def _tool_hover(self, **kw) -> dict:
        self.rate.check()
        move_ms = max(0, min(MAX_MOVE_MS, int(kw.get("move_ms") or 0)))
        if move_ms > 0:
            moved = await self._stepped_move_to_absolute(
                kw["x"], kw["y"], move_ms,
            )
        else:
            moved = await self._move_to_absolute(kw["x"], kw["y"])
        # If the move failed (cursor unavailable / no convergence / dropped
        # report) report that — don't idle, and don't claim ``ok: True`` for
        # a hover that never reached the target.
        if self._move_failed(moved):
            return moved
        await asyncio.sleep(max(0, min(10_000, int(kw.get("duration_ms", 500)))) / 1000.0)
        # ``moved`` already carries ``ok: True`` from the cursor-verified
        # converge stage; surface it as-is.
        return moved

    async def _tool_type(self, **kw) -> dict:
        self.rate.check()
        text = str(kw["text"])
        if len(text) > MAX_TYPE_LEN:
            raise ValueError(f"text too long ({len(text)} > {MAX_TYPE_LEN})")
        ok = await self.bridge.type_text(text)
        # Report characters actually sent: type_text strips control bytes
        # (newline/tab/…) by default, so the wire count can be lower than
        # len(text). Reporting the raw length would tell the agent "typed N
        # chars" when some were silently dropped (e.g. a lone "\n").
        sent = sum(1 for ch in text if not (ch < " " or ch == "\x7f"))
        return {"ok": ok, "chars": sent}

    async def _tool_scroll(self, **kw) -> dict:
        self.rate.check()
        delta = int(kw["delta"])
        ok = await self.bridge.mouse_scroll(delta)
        return {"ok": ok, "delta": delta}

    def _maybe_warn_self_interrupt(self, modifiers: list[str], key: str) -> None:
        """One-shot stderr heads-up the first time a quit/close combo is
        sent (see _is_self_interrupt_combo). Warn-only: the keystroke still
        goes out — blocking would break the legitimate remote-target case."""
        if self._warned_self_interrupt or not _is_self_interrupt_combo(modifiers, key):
            return
        self._warned_self_interrupt = True
        combo = "+".join([*modifiers, key]) if modifiers else key
        logger.warning(
            "sent a quit/close combo (%s). USB HID has no app targeting — "
            "keystrokes hit whatever window is frontmost. If this server "
            "shares a machine with your agent (Claude Code / Cursor / ...) "
            "and the agent is focused, this can quit the agent itself "
            "mid-task. Mitigate: hid.click the target window first, or drive "
            "a remote target (Pico 2 W). See INTEGRATIONS.md 'known footgun: "
            "self-interrupt'. (shown once per session)",
            combo,
        )

    def _split_key_shorthand(
        self, key_str: str, modifiers_arg: list[str] | None,
    ) -> tuple[list[str], str]:
        """Normalise a key + modifiers pair, splitting shorthand like
        ``"ctrl+c"`` / ``"ctrl+alt+l"`` into (modifiers, key) when every
        ``"+"``-separated head token is a known modifier name. Keeps
        ``"+"`` itself usable as a literal key. Shared by ``hid.key`` and
        the ``hid.batch`` ``key`` op so both parse shortcuts identically."""
        modifiers = [m.lower() for m in (modifiers_arg or [])]
        if "+" in key_str and len(key_str) > 1:
            parts = key_str.split("+")
            head, tail = parts[:-1], parts[-1]
            if tail and all(p.lower() in _MODIFIER_NAMES for p in head):
                modifiers = list(dict.fromkeys(modifiers + [p.lower() for p in head]))
                key_str = tail
        return modifiers, key_str

    async def _tool_key(self, **kw) -> dict:
        self.rate.check()
        modifiers, key_str = self._split_key_shorthand(
            str(kw["key"]), kw.get("modifiers"),
        )
        self._maybe_warn_self_interrupt(modifiers, key_str)
        ok = await self.bridge.key_combo(modifiers, key_str)
        return {"ok": ok}

    async def _tool_release_all(self, **_kw) -> dict:
        ok = await self.bridge.release_all()
        return {"ok": ok}

    # ── v1.1 tool handlers ──

    async def _tool_mouse_button_down(self, **kw) -> dict:
        self.rate.check()
        button = str(kw.get("button", "left"))
        ok = await self.bridge.mouse_button_down(button=button)
        return {"ok": ok, "button": button, "state": "down"}

    async def _tool_mouse_button_up(self, **kw) -> dict:
        self.rate.check()
        button = str(kw.get("button", "left"))
        ok = await self.bridge.mouse_button_up(button=button)
        return {"ok": ok, "button": button, "state": "up"}

    async def _tool_drag(self, **kw) -> dict:
        """Composed drag: move-to-source → press → glided-move-to-dest → release.

        Every sub-call's ACK is checked. If the move to the *source* fails
        (cursor unavailable / no convergence / a move report not ACKed) we
        abort BEFORE pressing — pressing and dragging from an unconfirmed
        position is worse than not dragging at all. Once the button is
        down, the press / drag / release ACKs are AND-ed into the final
        ``ok``, and the release runs in ``finally`` so a mid-drag exception
        can't leave the button stuck. A successful release never *upgrades*
        a failed drag back to ``ok: True``.
        """
        self.rate.check()
        button = str(kw.get("button", "left"))
        move_ms = max(0, min(MAX_MOVE_MS, int(kw.get("move_ms") or 300)))
        relative = bool(kw.get("relative", False))
        from_x, from_y = int(kw["from_x"]), int(kw["from_y"])
        to_x, to_y = int(kw["to_x"]), int(kw["to_y"])

        # Step 1: move to source (absolute or relative; snap, no glide here —
        # the glide budget is for the held-button move). Abort before
        # pressing if the source move did not land.
        if relative:
            src_ok = await self.bridge.mouse_move(from_x, from_y, relative=True)
            if not src_ok:
                return {
                    "ok": False,
                    "stage": "move_to_source",
                    "button": button,
                    "relative": True,
                    "from_x": from_x, "from_y": from_y,
                    "hint": (
                        "drag aborted: the move to the source point was not "
                        "ACKed by the firmware, so the button was never "
                        "pressed. Inspect the bridge diagnostic and retry."
                    ),
                }
            src_info = {"from_x": from_x, "from_y": from_y, "relative": True}
        else:
            moved = await self._move_to_absolute(from_x, from_y)
            if self._move_failed(moved):
                moved.setdefault("stage", "move_to_source")
                return moved
            src_info = {
                "from_x": moved.get("x", from_x),
                "from_y": moved.get("y", from_y),
                "from_target_x": from_x,
                "from_target_y": from_y,
                "from_converged": moved.get("converged"),
            }

        # Step 2: press the button.
        down_ok = await self.bridge.mouse_button_down(button=button)

        # Step 3: glided move to destination — with try/finally so a
        # mid-drag exception still releases the button (button-stuck
        # would be a worse user experience than the partial drag).
        dest_info: dict[str, Any] = {}
        dest_ok = True
        try:
            if relative:
                if move_ms > 0:
                    dest_info = await self._stepped_relative_move(to_x, to_y, move_ms)
                else:
                    m_ok = await self.bridge.mouse_move(to_x, to_y, relative=True)
                    dest_info = {"to_x": to_x, "to_y": to_y, "relative": True, "ok": m_ok}
            else:
                if move_ms > 0:
                    moved = await self._stepped_move_to_absolute(to_x, to_y, move_ms)
                else:
                    moved = await self._move_to_absolute(to_x, to_y)
                if self._move_failed(moved):
                    dest_info = moved
                else:
                    dest_info = {
                        "to_x": moved.get("x", to_x),
                        "to_y": moved.get("y", to_y),
                        "to_target_x": to_x,
                        "to_target_y": to_y,
                        "to_converged": moved.get("converged"),
                    }
            dest_ok = not self._move_failed(dest_info)
        finally:
            # Step 4: release. Idempotent on the firmware side — safe to
            # call even if step 3 raised before any movement.
            up_ok = await self.bridge.mouse_button_up(button=button)

        # Combine all sub-call ACKs. ``ok`` is set LAST so a stray ``ok``
        # spread in from ``dest_info`` (e.g. stepped-relative result) can't
        # override the authoritative combined verdict.
        out: dict[str, Any] = {
            "button": button,
            "move_ms": move_ms,
            "down_acked": bool(down_ok),
            "up_acked": bool(up_ok),
            **src_info,
            **dest_info,
        }
        out["ok"] = bool(down_ok) and bool(dest_ok) and bool(up_ok)
        return out

    async def _tool_key_press(self, **kw) -> dict:
        self.rate.check()
        key = str(kw["key"])
        modifiers = [m.lower() for m in (kw.get("modifiers") or [])]
        self._maybe_warn_self_interrupt(modifiers, key)
        ok = await self.bridge.key_press(key, modifiers)
        return {"ok": ok, "key": key, "modifiers": modifiers, "state": "down"}

    async def _tool_key_release(self, **kw) -> dict:
        self.rate.check()
        key = str(kw.get("key") or "")
        modifiers = [m.lower() for m in (kw.get("modifiers") or [])]
        ok = await self.bridge.key_release(key, modifiers)
        return {"ok": ok, "key": key or "<all>", "modifiers": modifiers, "state": "up"}

    async def _tool_hold_key(self, **kw) -> dict:
        """Composed hold: press → sleep → release. try/finally on release
        so a mid-hold exception still releases the key (stuck modifier
        would corrupt subsequent input on the host).

        Both the press and the release ACKs are checked: ``ok`` is True
        only when both landed. A successful release never masks a failed
        press (an un-pressed key reported as held would mislead the agent).
        """
        self.rate.check()
        key = str(kw["key"])
        duration_ms = max(1, min(10_000, int(kw.get("duration_ms", 500))))
        modifiers = [m.lower() for m in (kw.get("modifiers") or [])]
        self._maybe_warn_self_interrupt(modifiers, key)
        press_ok = await self.bridge.key_press(key, modifiers)
        release_ok = False
        try:
            await asyncio.sleep(duration_ms / 1000.0)
        finally:
            release_ok = await self.bridge.key_release(key, modifiers)
        return {
            "ok": bool(press_ok) and bool(release_ok),
            "key": key,
            "modifiers": modifiers,
            "duration_ms": duration_ms,
            "press_acked": bool(press_ok),
            "release_acked": bool(release_ok),
        }

    # ── hid.batch — sequence a short pre-planned action list ──
    #
    # Design notes (the four things that make this safe, not just fast):
    #  1. Rate limit is checked ONCE for the whole batch, then each op
    #     dispatches to the SAME leaf helpers the standalone tools use
    #     (_move_to_absolute / _stepped_* / bridge.*), which do not
    #     self-rate-check — so a mid-batch op can never raise a rate
    #     RuntimeError. cap10 vs 20 ops/sec never trips the limiter, but
    #     accounting once (not per op) is the intended design.
    #  2. Every op runs inside try/except so a raised exception (rate
    #     limit, "text too long" ValueError, HidUnavailableError) becomes
    #     that op's {ok: False, error, bridge_diagnostic} instead of
    #     escaping to dispatch's generic catch — which would discard all
    #     the per-op results gathered so far.
    #  3. The whole loop is wrapped in try/finally: on ABNORMAL
    #     termination (an op failed and stop_on_error halted the run)
    #     where a button/key was pressed, release_all fires so a held
    #     button/modifier can't poison the host's subsequent input. A
    #     clean run does NOT auto-release — a batch may intentionally
    #     leave state held for a follow-up call (mirrors hid.mouse_button_down).
    #  4. The return ALWAYS carries a top-level `ok` (= all ops ok). The
    #     dispatch layer keys isError off that top-level ok, so a partial
    #     failure (3/10 ok) surfaces as isError:true with the full results
    #     array intact — without it the agent would be told the whole
    #     batch succeeded. (Note: _on_tool_call only injects
    #     bridge_diagnostic into the top-level dict, so per-op failures
    #     attach their own diagnostic here.)
    #
    # Convergence / ACK logic is REUSED via the leaf helpers, never
    # re-implemented, so each op inherits the failure-propagation contract
    # pinned by test_composed_tool_failure_propagation.

    def _batch_op_error(self, index: int, op: dict, exc: Exception) -> dict:
        """Convert an exception raised while running one op into that op's
        structured failure entry, pulling in the bridge's specific
        diagnostic when present (the dispatch layer only injects that for
        the top-level dict, not array elements)."""
        out: dict[str, Any] = {
            "index": index,
            "type": op.get("type"),
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        br = getattr(self, "bridge", None)
        br_detail = getattr(br, "last_error_detail", None) if br else None
        if br_detail:
            out["bridge_diagnostic"] = br_detail
        return out

    async def _run_batch_op(self, index: int, op: dict) -> dict:
        """Execute ONE batch op through the standalone tools' leaf helpers
        so converge / ACK / click-gate semantics are identical and live in
        exactly one place. Returns a per-op dict that always carries
        {index, type, ok}. May raise — the caller wraps this in try/except
        and routes any exception through _batch_op_error."""
        t = op.get("type")
        base: dict[str, Any] = {"index": index, "type": t}
        move_ms = max(0, min(MAX_MOVE_MS, int(op.get("move_ms") or 0)))

        if t in ("move", "click"):
            relative = bool(op.get("relative", False))
            if relative:
                dx, dy = int(op["x"]), int(op["y"])
                if move_ms > 0:
                    r = await self._stepped_relative_move(dx, dy, move_ms)
                else:
                    ok = await self.bridge.mouse_move(dx, dy, relative=True)
                    r = {"ok": ok, "dx": dx, "dy": dy, "relative": True}
            else:
                if move_ms > 0:
                    r = await self._stepped_move_to_absolute(
                        int(op["x"]), int(op["y"]), move_ms,
                    )
                else:
                    r = await self._move_to_absolute(int(op["x"]), int(op["y"]))
            # Cursor-unavailable structured error → this op failed; carry
            # the reason through and force ok:False.
            if "error" in r:
                return {**base, **r, "ok": False}
            if t == "click":
                if self._move_failed(r):
                    # Positioning failed — do NOT click somewhere we never
                    # confirmed reaching; surface the move failure.
                    return {**base, **r, "ok": False}
                click_ok = await self.bridge.mouse_click(
                    button=str(op.get("button", "left")),
                    double=bool(op.get("double", False)),
                )
                r["clicked"] = click_ok
                r["ok"] = click_ok
            return {**base, **r}

        if t == "button_down":
            button = str(op.get("button", "left"))
            ok = await self.bridge.mouse_button_down(button=button)
            return {**base, "ok": ok, "button": button, "state": "down"}

        if t == "button_up":
            button = str(op.get("button", "left"))
            ok = await self.bridge.mouse_button_up(button=button)
            return {**base, "ok": ok, "button": button, "state": "up"}

        if t == "key":
            modifiers, key_str = self._split_key_shorthand(
                str(op["key"]), op.get("modifiers"),
            )
            self._maybe_warn_self_interrupt(modifiers, key_str)
            ok = await self.bridge.key_combo(modifiers, key_str)
            return {**base, "ok": ok}

        if t == "type":
            text = str(op["text"])
            if len(text) > MAX_TYPE_LEN:
                raise ValueError(f"text too long ({len(text)} > {MAX_TYPE_LEN})")
            ok = await self.bridge.type_text(text)
            sent = sum(1 for ch in text if not (ch < " " or ch == "\x7f"))
            return {**base, "ok": ok, "chars": sent}

        if t == "scroll":
            delta = int(op["delta"])
            ok = await self.bridge.mouse_scroll(delta)
            return {**base, "ok": ok, "delta": delta}

        raise ValueError(f"unknown batch op type: {t!r}")

    def _op_settle_ms(self, op: dict) -> int:
        """Effective pause (ms) to apply AFTER this op. When delay_ms is
        omitted, click/button ops get DEFAULT_CLICK_SETTLE_MS so the OS
        doesn't merge/drop back-to-back clicks (real macOS dogfood); other
        op types get 0. An explicit delay_ms (including 0) overrides the
        default. Coerces defensively — a non-int delay_ms must not crash
        the batch."""
        raw = op.get("delay_ms")
        if raw is None:
            return DEFAULT_CLICK_SETTLE_MS if op.get("type") in _SETTLE_OP_TYPES else 0
        try:
            return max(0, min(MAX_BATCH_DELAY_MS, int(raw)))
        except (TypeError, ValueError):
            return 0

    async def _tool_batch(self, **kw) -> dict:
        self.rate.check()                       # ① rate-check once for the batch
        ops = kw.get("ops")
        if ops is None:
            ops = []
        if not isinstance(ops, list):
            raise ValueError(f"ops must be an array, got {type(ops).__name__}")
        # Hard cap enforced HERE, not just in the schema: the server does
        # not validate tool args against inputSchema, and a raw JSON-RPC
        # client can bypass client-side validation entirely.
        if len(ops) > MAX_BATCH_OPS:
            raise ValueError(
                f"hid.batch accepts at most {MAX_BATCH_OPS} ops, got {len(ops)}"
            )
        stop_on_error = bool(kw.get("stop_on_error", True))
        results: list[dict] = []
        failed_index: Optional[int] = None
        stopped_early = False
        pressed_something = False               # any button_down issued?
        released = False
        cleanup_attempted = False
        cleanup_error: Optional[str] = None
        try:
            for i, op in enumerate(ops):
                if not isinstance(op, dict):
                    results.append({
                        "index": i, "type": None, "ok": False,
                        "error": f"op must be an object, got {type(op).__name__}",
                    })
                    if failed_index is None:
                        failed_index = i
                    if stop_on_error:
                        stopped_early = True
                        break
                    continue
                # Only button_down leaves HELD state needing cleanup. The
                # `key` op uses key_combo (atomic press+release — no held
                # key), and the held key_press primitive is intentionally
                # NOT exposed in batch; button_up needs no marking either.
                # Marking `key` here would fire a needless release_all and
                # mis-report released_all=True on an abnormal stop.
                if op.get("type") == "button_down":
                    pressed_something = True
                try:
                    r = await self._run_batch_op(i, op)       # ④ per-op try/except
                except Exception as e:
                    r = self._batch_op_error(i, op, e)
                else:
                    # Non-exception failure (a leaf returned ok:False — bridge
                    # timeout / seq mismatch / firmware ERROR / no convergence)
                    # carries no reason on its own; attach the bridge's specific
                    # diagnostic so EVERY per-op failure has the same context the
                    # exception path (_batch_op_error) already provides. The
                    # dispatch layer only injects this for the top-level dict,
                    # not array elements.
                    if not r.get("ok", False) and "bridge_diagnostic" not in r:
                        det = getattr(self.bridge, "last_error_detail", None)
                        if det:
                            r["bridge_diagnostic"] = det
                results.append(r)
                if not r.get("ok", False):
                    if failed_index is None:
                        failed_index = i
                    # Stop when the caller asked to (stop_on_error) OR when the
                    # device itself has gone non-responsive: a dead/unplugged
                    # device can't recover mid-batch, so continuing would only
                    # grind each remaining op through its full ACK timeout
                    # (~1 s each — a 10-op batch is ~minutes of dead-air, all
                    # while stdio is serial-blocked). Stopping also lets the
                    # finally below release any held button. A *recoverable*
                    # per-op error (bad arg / firmware ERROR / seq mismatch /
                    # no convergence) still honors stop_on_error as before.
                    if stop_on_error or self._looks_device_nonresponsive(r):
                        stopped_early = True
                        break
                # Inter-op settle. Click/button ops get a small DEFAULT gap so
                # the OS doesn't coalesce/drop back-to-back clicks (real macOS
                # dogfood: zero-gap adjacent clicks merged, only the last
                # registered). An explicit delay_ms (incl. 0) overrides the
                # default and is honored even after the final op; the implicit
                # default only fills GAPS between ops (no needless trailing
                # wait). The sleep is intentionally OUTSIDE the per-op
                # try/except: the op result is already recorded, and the only
                # thing asyncio.sleep raises is CancelledError (a BaseException)
                # — which must propagate to abort the batch, with the finally
                # below still running release_all cleanup on the way out.
                settle = self._op_settle_ms(op)
                explicit = op.get("delay_ms") is not None
                if settle and (explicit or i < len(ops) - 1):
                    await asyncio.sleep(settle / 1000.0)
        finally:
            # ③ Clean up held state on ANY abnormal end after a button_down:
            # either a stop_on_error halt (stopped_early) OR a
            # continue-on-error run that nonetheless had a failure
            # (failed_index set — e.g. a button_up that the firmware didn't
            # ACK, leaving the button physically held). A fully clean run
            # (failed_index is None) leaves the button down on purpose for a
            # follow-up call and is NOT auto-released. Without the
            # failed_index arm, a continue-on-error batch whose button_up
            # failed returned ok:False yet left the button stuck with no
            # cleanup and no signal.
            if pressed_something and (stopped_early or failed_index is not None):
                cleanup_attempted = True
                try:
                    await self.bridge.release_all()     # rate-exempt panic stop
                    released = True
                except Exception as e:
                    cleanup_error = f"{type(e).__name__}: {e}"
                    logger.warning(
                        "hid.batch cleanup release_all failed: %s",
                        cleanup_error, exc_info=True,
                    )
        result: dict[str, Any] = {
            "ok": len(results) == len(ops) and all(r.get("ok", False) for r in results),
            "count": len(results),
            "failed_index": failed_index,
            "stopped_early": stopped_early,
            "released_all": released,
            "results": results,
        }
        # Disambiguate released_all=False: "no cleanup needed" vs "cleanup
        # attempted but release_all itself failed" (e.g. hardware became
        # unavailable). Surface the latter so a held button isn't silently
        # left stuck. We do NOT re-raise — that would discard the per-op
        # results the batch is contractually obliged to return.
        if cleanup_attempted and not released:
            result["cleanup_error"] = cleanup_error or "release_all failed"
        return result

    async def _tool_device_list(self, **_kw) -> dict:
        return {"ports": list_pico_ports()}

    async def _tool_device_info(self, **_kw) -> dict:
        info = await self.bridge.device_info() if self.bridge else {}
        return {
            "info": info,
            "screen": {
                "width": self.config.screen_w,
                "height": self.config.screen_h,
                "source": self._screen_source,
            },
            "mcp_version": __version__,
        }

    def _probe_pillow(self):
        """Import PIL.Image and force its native ``_imaging`` extension to
        load. Isolated as a method so (a) the dlopen — where hardened-runtime
        library validation rejects non-platform extensions — happens in one
        place, and (b) tests can monkeypatch it to simulate that rejection."""
        from PIL import Image  # noqa: PLC0415 — lazy; _imaging dlopen here
        Image.new("RGB", (1, 1))  # force any deferred native init
        return Image

    def _resolve_screenshot_backend(self) -> str:
        """Pick (and cache) the screenshot encode backend.

        'auto' (default): use Pillow when its native _imaging loads, else fall
        back to the no-native 'mss-png' path (which works under hardened-
        runtime / library-validation Python hosts such as bundled launchers).
        Cached so a failing Pillow dlopen isn't retried on every screenshot.
        """
        if self._ss_backend is not None:
            return self._ss_backend
        forced = (self.config.screenshot_backend or "auto").lower()
        if forced not in ("auto", "pillow", "mss-png"):
            raise ValueError(
                "screenshot_backend must be auto|pillow|mss-png, got "
                f"{forced!r}"
            )
        if forced == "mss-png":
            self._ss_backend = "mss-png"
            return self._ss_backend
        try:
            self._ss_image = self._probe_pillow()
            self._ss_backend = "pillow"
        except Exception as e:  # ImportError (dlopen reject) / OSError / ...
            if forced == "pillow":
                raise RuntimeError(_screenshot_pillow_note(e, forced=True))
            self._ss_backend = "mss-png"
            self._ss_note = _screenshot_pillow_note(e, forced=False)
            logger.warning(self._ss_note)
        return self._ss_backend

    async def _tool_screenshot(self, **kw) -> "ImageResult":
        try:
            import mss  # type: ignore  # pure-Python; loads under lib validation
        except ImportError as e:
            raise RuntimeError(
                "screenshot needs mss: pip install "
                "'clawtouch-mcp[screenshot-min]' (mss only, no native deps) "
                "or '[screenshot]' (adds Pillow for JPEG/Retina downscale). "
                f"missing: {getattr(e, 'name', e)}"
            )
        backend = self._resolve_screenshot_backend()  # 'pillow' | 'mss-png'
        fmt = (kw.get("format") or "jpeg").lower()
        if fmt not in ("jpeg", "png"):
            raise ValueError(
                f"format must be 'jpeg' or 'png', got {fmt!r}"
            )
        notes: list = []
        if self._ss_note:
            notes.append(self._ss_note)
        if backend == "mss-png" and fmt == "jpeg":
            # JPEG encoding needs Pillow; on the no-Pillow backend return
            # PNG rather than failing the call.
            fmt = "png"
            notes.append(
                "JPEG needs Pillow; returned PNG on the mss-png backend."
            )
        # Cap measured on OUTPUT pixels (after auto-resize), not on the
        # raw mss grab — the previous design read monitor["width"] *
        # monitor["height"] which on macOS Retina is the LOGICAL point
        # count (e.g. 1512x982 = 1.48M) while the actual grab returned
        # PHYSICAL pixels (3024x1964 = 5.94M), letting a 24MB base64
        # PNG slip through and overflow Claude Desktop's tool-result
        # text buffer. After the resize-to-logical step below the cap
        # is meaningful again as a defence against giant region asks.
        MAX_OUTPUT_PIXELS = 4_000_000
        with mss.MSS() as sct:  # `mss.mss()` deprecated in mss 10.x
            primary = sct.monitors[1]
            if "region" in kw:
                if len(kw["region"]) != 4:
                    raise ValueError(
                        f"invalid region {kw['region']}: expected [x1,y1,x2,y2]"
                    )
                x1, y1, x2, y2 = (int(v) for v in kw["region"])
                if x2 <= x1 or y2 <= y1:
                    raise ValueError(
                        f"invalid region {kw['region']}: "
                        "need x2 > x1 and y2 > y1"
                    )
                # Clamp the region to the primary monitor's bounds —
                # previously an agent-supplied region with negative
                # offsets or huge sizes captured *across* monitors the
                # user might not have intended to expose. Restricting
                # to primary matches the same "primary only" semantics
                # used by --screen WxH auto-detect.
                left = primary.get("left", 0)
                top = primary.get("top", 0)
                right = left + primary["width"]
                bottom = top + primary["height"]
                cx1 = max(left, min(x1, right))
                cy1 = max(top, min(y1, bottom))
                cx2 = max(left, min(x2, right))
                cy2 = max(top, min(y2, bottom))
                if cx2 - cx1 < 1 or cy2 - cy1 < 1:
                    raise ValueError(
                        f"region {kw['region']} falls entirely outside "
                        f"the primary monitor ({left},{top})-({right},{bottom}) "
                        "after clamping"
                    )
                monitor = {"left": cx1, "top": cy1,
                           "width": cx2 - cx1, "height": cy2 - cy1}
            else:
                monitor = primary
            shot = sct.grab(monitor)
            raw_w, raw_h = shot.width, shot.height

            # Downsample policy (shared by BOTH backends): for full-screen
            # captures, collapse the physical buffer to LOGICAL resolution
            # when it's noticeably bigger than the configured screen size —
            # the Retina ~2x path on macOS / Windows >100% DPI; a no-op at
            # 100% DPI / Linux. 1.2x threshold tolerates fractional rounding.
            target_w, target_h = raw_w, raw_h
            if (not kw.get("region")
                    and self.config.screen_w and self.config.screen_h
                    and raw_w >= self.config.screen_w * 1.2):
                target_w = self.config.screen_w
                target_h = self.config.screen_h

            # Cap output pixels (4K at 1×, or an absurd region) so the
            # base64 payload can't overflow the MCP client text buffer.
            if target_w * target_h > MAX_OUTPUT_PIXELS:
                ratio = (MAX_OUTPUT_PIXELS / (target_w * target_h)) ** 0.5
                target_w = max(1, int(target_w * ratio))
                target_h = max(1, int(target_h * ratio))

            if backend == "pillow":
                Image = self._ss_image
                img = Image.frombytes("RGB", (raw_w, raw_h), shot.rgb)
                if (target_w, target_h) != (raw_w, raw_h):
                    img = img.resize((target_w, target_h), Image.LANCZOS)
                buf = io.BytesIO()
                if fmt == "jpeg":
                    img.save(buf, format="JPEG", quality=80, optimize=True)
                    mime = "image/jpeg"
                else:
                    img.save(buf, format="PNG", optimize=True)
                    mime = "image/png"
                image_bytes = buf.getvalue()
                out_w, out_h = target_w, target_h
            else:  # mss-png — no native extension; library-validation safe
                # Pure-Python integer-stride decimation to (≈) target, then
                # mss's pure-Python zlib PNG encoder. Can't do fractional DPI
                # or JPEG, but loads where Pillow's _imaging is blocked.
                out_w, out_h, rgb = _decimate_rgb(
                    shot.rgb, raw_w, raw_h, target_w, target_h)
                image_bytes = mss.tools.to_png(rgb, (out_w, out_h))
                mime = "image/png"

            # Screenshot pixel space vs click-coordinate space. Both
            # backends downsample full-screen Retina to ~logical, so on
            # the common path scale_x/y collapse to ~1.0; on mss-png with
            # fractional DPI (can't integer-decimate exactly) the scale is
            # reported honestly so callers divide correctly. Always emit
            # them so agent code that divides by scale stays correct.
            scale_x = 1.0
            scale_y = 1.0
            if (self.config.screen_w and self.config.screen_h
                    and not kw.get("region")):
                scale_x = out_w / self.config.screen_w
                scale_y = out_h / self.config.screen_h

        metadata = {
            "width": out_w,
            "height": out_h,
            "scale_x": round(scale_x, 4),
            "scale_y": round(scale_y, 4),
            "format": fmt,
            "mime_type": mime,
            "size_bytes": len(image_bytes),
            # What mss originally grabbed — agents can tell a resize
            # happened (raw_size != width/height). ~2x on Retina.
            "raw_size": [raw_w, raw_h],
            # Which encode path produced this. 'mss-png' = the no-Pillow
            # fallback (e.g. a hardened-runtime library-validation host).
            "backend": backend,
        }
        if notes:
            metadata["note"] = " ".join(notes)
        return ImageResult(
            image_bytes=image_bytes,
            mime_type=mime,
            metadata=metadata,
        )

    # ═══════════════════ JSON-RPC dispatch ═══════════════════

    async def dispatch(self, msg: dict) -> Optional[dict]:
        """Return a response dict, or None for notifications.

        MCP spec compliance note: tool *execution* failures must come
        back as ``result.content + isError:true`` (so the agent sees
        the error and can react), NOT as JSON-RPC errors. JSON-RPC
        error codes are reserved for protocol-layer failures —
        unknown method (-32601), bad params (-32602), internal
        dispatch crash (-32603 / -32000). `_on_tool_call` enforces
        this by catching handler exceptions itself; this method's
        ``except Exception`` only catches genuine protocol-layer
        failures left over after that.
        """
        # Valid JSON that isn't a JSON-RPC *object* — `[]`, `"x"`, `5`,
        # `true`, `null` — would otherwise raise AttributeError on the
        # msg.get() calls below. json.loads already succeeded, so run_stdio's
        # parse-error guard (which only catches JSONDecodeError/ValueError)
        # misses it, and the AttributeError reaches run_stdio's outer
        # `except Exception: ...; raise` — killing the whole session over one
        # bad line. Per JSON-RPC 2.0 a non-Request payload is -32600 Invalid
        # Request; id=None because there's no parsable id to echo (same as the
        # -32700 parse-error path).
        if not isinstance(msg, dict):
            return _error_response(None, -32600, "Invalid Request: message must be a JSON object")
        jid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params")
        if params is None:
            params = {}
        elif not isinstance(params, dict):
            # Malformed params → JSON-RPC -32602 Invalid params, NOT -32603
            # Internal error (which would mislead a client into thinking the
            # server crashed) and without leaking the raw Python AttributeError.
            # A Notification (no id) is never replied to, even when malformed.
            if jid is None:
                return None
            return _error_response(jid, -32602, "Invalid params: must be an object")
        try:
            if method == "initialize":
                result = self._on_initialize(params)
            elif method == "notifications/initialized":
                self._initialized = True
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._on_tools_list()
            elif method == "tools/call":
                result = await self._on_tool_call(params)
            elif method == "shutdown":
                # Tell run_stdio to exit; reply first, then it'll
                # observe the event on its next loop pass.
                self._stop_event.set()
                result = {}
            elif method in ("notifications/cancelled", "notifications/exit"):
                # Notifications we accept silently — no response.
                if method == "notifications/exit":
                    self._stop_event.set()
                return None
            else:
                # Never reply to a Notification (JSON-RPC 2.0 §4.1), even an
                # unhandled one — only a request (with an id) gets -32601.
                # MCP clients legitimately send notifications this server
                # doesn't handle (progress, roots/list_changed, future spec).
                if jid is None:
                    return None
                return _error_response(jid, -32601, f"method not found: {method}")
            if jid is None:
                return None  # notification — no response
            return {"jsonrpc": "2.0", "id": jid, "result": result}
        except Exception as e:
            logger.exception("dispatch error for %s", method)
            return _error_response(jid, -32603, str(e))

    def _on_initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "instructions": (
                "ClawTouch MCP exposes physical USB HID keyboard and mouse "
                "input to the host machine via a Raspberry Pi Pico 2 board. "
                "Prefer hid.* tools when (1) no API or automation path "
                "exists for the target application (e.g. legacy desktop "
                "software with no scripting interface, GUI-only workflows), "
                "or (2) the user explicitly asks for physical keyboard/mouse "
                "interaction. For tasks that can be solved with file APIs, "
                "browser automation, or OS APIs, prefer those instead. The "
                "device.* tools are read-only diagnostics for the underlying "
                "USB connection."
            ),
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": "clawtouch-mcp",
                "version": __version__,
            },
        }

    def _on_tools_list(self) -> dict:
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                }
                for t in self.tools.values()
            ]
        }

    async def _on_tool_call(self, params: dict) -> dict:
        # Release-on-idle: 每次 tool call 重置 idle 计时 + 确保 watcher 在跑
        # (lazy 启动 — 没人用 mcp 就不计时, 不释放, 不占额外资源)
        self._last_used_at = time.monotonic()
        self._ensure_idle_watch_started()
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = self.tools.get(name)
        if tool is None:
            # MCP spec: unknown tool is a tool-level error, not a
            # protocol error — return isError:true content so the
            # agent sees a descriptive message instead of a generic
            # JSON-RPC failure.
            return {
                "content": [{
                    "type": "text",
                    "text": f"unknown tool: {name!r} — available: "
                            f"{sorted(self.tools.keys())}",
                }],
                "isError": True,
            }
        # MCP spec compliance: tool *execution* failures (rate limit,
        # bridge timeout, hardware unavailable, bad args validated by
        # the handler, …) must come back as `isError:true` content
        # so the agent can read the message and react. JSON-RPC error
        # codes are reserved for protocol-layer faults. Previously
        # every ValueError / RuntimeError bubbled to dispatch's
        # `except Exception` and became a JSON-RPC -32000, hiding the
        # actual cause from compliant clients (Claude Desktop, Cline).
        self._inflight_handlers += 1
        try:
            try:
                result = await tool.handler(**args)
            finally:
                self._inflight_handlers -= 1
                # Refresh idle timestamp on exit too — a long-running
                # handler shouldn't be "stale" the instant it returns.
                self._last_used_at = time.monotonic()
        except Exception as e:
            logger.warning("tool %s exec error: %s", name, e)
            error_text = f"{type(e).__name__}: {e}"
            # If the bridge has a more specific diagnostic (timeout /
            # seq mismatch / firmware ERROR code / parse error), pull
            # it in — that's what the agent actually needs to retry.
            br = getattr(self, "bridge", None)
            br_detail = getattr(br, "last_error_detail", None) if br else None
            if br_detail:
                error_text = f"{error_text}\nbridge diagnostic: {br_detail}"
            return {
                "content": [{"type": "text", "text": error_text}],
                "isError": True,
            }
        # Handler returned a structured-error dict (e.g. cursor
        # tracking unavailable). Echo that into isError content too.
        if isinstance(result, dict) and "error" in result:
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False),
                }],
                "isError": True,
            }
        # Bridge-level failure (timeout / seq mismatch / firmware ERROR /
        # parse error) currently surfaces as ``{"ok": False, ...}`` from
        # the high-level bridge methods — they swallow the underlying
        # exception and return False so adapter code can keep flowing.
        # That's the right call at the bridge layer, but at the MCP
        # boundary it would let a hardware failure ride out as
        # ``isError: false`` and the agent would think the click landed.
        # MCP spec § Tool Result requires execution failures be flagged
        # as ``isError: true``. We pull the specific diagnostic from
        # ``bridge.last_error_detail`` (set by ``_send_raw``) so the
        # agent sees *why* the call failed, not just that ``ok`` was
        # False.
        if isinstance(result, dict) and result.get("ok") is False:
            br = getattr(self, "bridge", None)
            br_detail = getattr(br, "last_error_detail", None) if br else None
            payload = dict(result)
            if br_detail:
                payload["bridge_diagnostic"] = br_detail
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }],
                "isError": True,
            }
        # Image-bearing tool results (hid.screenshot) take the MCP
        # standard image content path so the client renders them as
        # vision tokens instead of trying to fit a multi-MB base64
        # string into the tool-result text envelope. The metadata
        # (width/height/scale_x/etc.) rides alongside as text so the
        # agent can still introspect dimensions.
        if isinstance(result, ImageResult):
            import base64 as _b64
            return {
                "content": [
                    {
                        "type": "image",
                        "data": _b64.b64encode(result.image_bytes).decode("ascii"),
                        "mimeType": result.mime_type,
                    },
                    {
                        "type": "text",
                        "text": json.dumps(result.metadata, ensure_ascii=False),
                    },
                ],
                "isError": False,
            }
        return {
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
            ],
            "isError": False,
        }


def _error_response(jid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": jid, "error": {"code": code, "message": message}}


# ═══════════════════ stdio framing ═══════════════════
#
# Windows ProactorEventLoop + asyncio.connect_read_pipe(sys.stdin) hits
# OSError [WinError 6] because CreateIoCompletionPort refuses anonymous
# pipe handles. Same code path (StreamReaderProtocol over stdin) works on
# POSIX SelectorEventLoop. To stay cross-platform we read stdin in a
# worker thread via asyncio.to_thread — performance is fine for MCP
# traffic (single-digit req/s) and the code is identical on every OS.

async def _read_line() -> bytes:
    return await asyncio.to_thread(sys.stdin.buffer.readline)


async def _read_exact(n: int) -> bytes:
    """Read exactly n bytes, looping until satisfied or EOF."""
    def _read_blocking() -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sys.stdin.buffer.read(n - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)
    return await asyncio.to_thread(_read_blocking)


def _write_message(writer: io.TextIOBase, msg: dict, *, framed: bool) -> None:
    data = json.dumps(msg, ensure_ascii=False)
    if framed:
        body = data.encode("utf-8")
        writer.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))  # type: ignore[attr-defined]
        writer.buffer.write(body)  # type: ignore[attr-defined]
    else:
        # Line-delimited (newline) mode. Write UTF-8 *bytes* via .buffer,
        # the same as the framed branch — NOT text via the locale-encoded
        # TextIOWrapper. On a non-UTF-8 console code page (cp936 / GBK on
        # Chinese Windows, where sys.stdout.encoding defaults to 'gbk' for
        # a piped stdout) `writer.write(data)` re-encodes any non-ASCII in
        # the JSON to GBK — a single em-dash in a tool description is
        # enough — and a UTF-8 MCP client then fails to decode the frame.
        # JSON-RPC over stdio is UTF-8 by spec regardless of host locale.
        writer.buffer.write((data + "\n").encode("utf-8"))  # type: ignore[attr-defined]
    writer.flush()


async def _read_framed(length: int) -> dict:
    # Reject obviously bogus lengths before allocating. Negative is
    # nonsense, zero would never carry a JSON-RPC payload, and anything
    # over MAX_FRAME_LEN is either a runaway client or a tampered header.
    # ValueError propagates up to run_stdio's parse-error handler and
    # becomes a JSON-RPC -32700, keeping the session alive.
    if length <= 0:
        raise ValueError(f"invalid Content-Length: {length}")
    if length > MAX_FRAME_LEN:
        raise ValueError(
            f"Content-Length {length} exceeds MAX_FRAME_LEN ({MAX_FRAME_LEN}B)"
        )
    body = await _read_exact(length)
    # _read_exact returns a SHORT buffer on EOF (its loop breaks when the
    # stream closes mid-frame). Without this check a truncated body whose
    # prefix is coincidentally complete valid JSON would be processed as a
    # whole message. Treat a short read as a parse error — ValueError lands
    # in run_stdio's `except (json.JSONDecodeError, ValueError)` → -32700,
    # session stays alive (consistent with the length guards above).
    if len(body) != length:
        raise ValueError(f"short frame: got {len(body)} of {length} bytes")
    return json.loads(body)


async def _read_one(framed: bool) -> Optional[dict]:
    """Read one message in the established framing. Returns None on EOF."""
    if framed:
        # skip blanks, find Content-Length header
        while True:
            header = await _read_line()
            if not header:
                return None
            htext = header.decode("utf-8", errors="replace").strip()
            if htext.lower().startswith("content-length:"):
                length = int(htext.split(":", 1)[1].strip())
                # consume header block until blank line
                while True:
                    more = await _read_line()
                    if not more or more in (b"\r\n", b"\n"):
                        break
                return await _read_framed(length)
            # ignore unknown header lines
    else:
        while True:
            line = await _read_line()
            if not line:
                return None
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                return json.loads(text)


async def _dispatch_and_write(
    server: ClawTouchMcpServer, msg: dict, framed: bool,
) -> None:
    resp = await server.dispatch(msg)
    if resp is not None:
        _write_message(sys.stdout, resp, framed=framed)


async def run_stdio(server: ClawTouchMcpServer) -> None:
    """Main loop: read stdin, dispatch, write stdout.

    A single malformed JSON line used to crash this loop with
    JSONDecodeError → the whole MCP session died and clients saw the
    process exit. Per JSON-RPC 2.0 spec, malformed JSON should come
    back as ``error.code = -32700`` and the connection should remain
    open. We now catch the parse error per-message, write a -32700
    response, and continue.
    """
    n_hid = sum(1 for n in server.tools if n.startswith("hid."))
    n_device = sum(1 for n in server.tools if n.startswith("device."))
    logger.info(
        "%d HID tools + %d device tools registered; listening on stdio",
        n_hid, n_device,
    )
    # Decide framing on first message
    framed: Optional[bool] = None
    try:
        while True:
            if server._stop_event.is_set():
                return
            raw = await _read_line()
            if not raw:
                return
            first = raw.decode("utf-8", errors="replace").strip()
            if not first:
                continue
            try:
                if first.lower().startswith("content-length:"):
                    framed = True
                    length = int(first.split(":", 1)[1].strip())
                    while True:
                        more = await _read_line()
                        if not more or more in (b"\r\n", b"\n"):
                            break
                    msg = await _read_framed(length)
                else:
                    framed = False
                    msg = json.loads(first)
            except (json.JSONDecodeError, ValueError) as e:
                # Per JSON-RPC 2.0: parse error → -32700, id=null
                # (we have no parsed id to echo). Keep the connection
                # alive — the next line might be valid.
                _write_message(
                    sys.stdout,
                    _error_response(None, -32700, f"parse error: {e}"),
                    framed=bool(framed),
                )
                framed = None  # framing not yet established; retry next msg
                continue

            await _dispatch_and_write(server, msg, framed)

            # Subsequent messages use the same framing
            while not server._stop_event.is_set():
                try:
                    msg = await _read_one(framed)
                except (json.JSONDecodeError, ValueError) as e:
                    _write_message(
                        sys.stdout,
                        _error_response(None, -32700, f"parse error: {e}"),
                        framed=framed,
                    )
                    continue
                if msg is None:
                    return
                await _dispatch_and_write(server, msg, framed)
            return
    except asyncio.IncompleteReadError:
        return
    except Exception:
        logger.exception("stdio loop crashed")
        raise
