"""MCP stdio server exposing 10 HID tools.

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
from .bridge import SerialHidBridge, auto_detect_port, auto_detect_ports, list_pico_ports
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
# Closed-loop convergence constants for absolute cursor moves.
# macOS pointer ballistics non-linearly scales single HID deltas
# (measured ~110% in low-speed segment on Ventura ARM64), so a single
# fire-and-forget mouse_move overshoots/undershoots by 10-90 px and
# returns ok=true while the cursor is still drifting. We iterate:
# query OS cursor → compute residual → send delta → settle → repeat.
# Per-pass residual shrinks to ~30% of previous (empirically).
#   - MOVE_TOLERANCE=3 px: macOS reports quantize to ±2 px; tighter
#     would spin forever on intrinsic jitter.
#   - MOVE_MAX_ITERS=4: pass 4 lands ≤2 px in measured runs; further
#     iterations have <1 px marginal benefit.
#   - MOVE_SETTLE_MS=20: ~2× macOS HID report cycle (8-10 ms).
MOVE_TOLERANCE = 3
MOVE_MAX_ITERS = 4
MOVE_SETTLE_MS = 20
# Upper bound on the Content-Length header of an incoming framed
# JSON-RPC message. The MCP spec allows arbitrary message sizes but in
# practice every reasonable tool call fits in well under 1 MB; capping
# at 16 MB keeps a single bad/malicious header from making _read_exact
# allocate gigabytes before EOF. Returns -32700 parse error on overrun.
MAX_FRAME_LEN = 16 * 1024 * 1024
_MODIFIER_NAMES = frozenset({"ctrl", "shift", "alt", "gui", "win", "cmd"})


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

    async def type_text(self, text: str, *, chunk_size: int = 32) -> bool:
        self._calls.append(("type", {"text": text}))
        return True

    async def key_combo(self, modifiers: list[str], key: str) -> bool:
        self._calls.append(("key", {"modifiers": modifiers, "key": key}))
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

    async def type_text(self, text: str, *, chunk_size: int = 32) -> bool:
        return await self._try_or_fail("type_text", text, chunk_size=chunk_size)

    async def key_combo(self, modifiers: list[str], key: str) -> bool:
        return await self._try_or_fail("key_combo", modifiers, key)

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


# ═══════════════════ Server ═══════════════════

class ClawTouchMcpServer:
    def __init__(self, config: ServerConfig):
        self.config = config

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
        self.bridge: Any = None
        self.rate = RateLimiter(config.ops_per_sec)
        self.tools: dict[str, Tool] = {}
        self._initialized = False
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

    # ── Lifecycle ──

    async def start(self) -> None:
        if self.config.mock:
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
                "delta → settle, up to 4 iterations, ≤3 px tolerance) "
                "to absorb OS pointer-ballistics non-linearity (macOS "
                "scales single HID deltas ~110% in the low-speed "
                "segment, so a fire-and-forget move overshoots by "
                "10-90 px). The returned `x`/`y` are the actual "
                "landing coordinates; `target_x`/`target_y` echo the "
                "request; `converged: true` means residual ≤3 px. "
                "Click fires regardless of convergence — inspect "
                "`converged` if you need to retry on missed targets.\n\n"
                "Optional `move_ms` switches to glide mode: the move "
                "is broken into ~10 ms HID reports over N ms (linear "
                "interpolation, then a 3-iter converge to clean up "
                "the final landing). Default 0 = snap mode."
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
            description="Type a string as if on a physical keyboard (US layout).",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=self._tool_type,
        ))
        self._register(Tool(
            name="hid.scroll",
            description="Scroll the mouse wheel. Positive=up, negative=down.",
            input_schema={
                "type": "object",
                "properties": {"delta": {"type": "integer"}},
                "required": ["delta"],
            },
            handler=self._tool_scroll,
        ))
        self._register(Tool(
            name="hid.key",
            description="Press a key or keyboard shortcut.",
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
            description="Release every held key / mouse button (panic stop).",
            input_schema={"type": "object", "properties": {}},
            handler=self._tool_release_all,
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
                    "Take a screenshot (requires --allow-screenshot + the "
                    "'[screenshot]' extras: mss + Pillow). The image is "
                    "returned as MCP image content (vision-token path) so "
                    "Retina captures don't overflow the tool-result text "
                    "buffer. Default format is JPEG q80 — for pixel-perfect "
                    "OCR-style work pass format='png'. "
                    "Captured at the logical-point space on high-DPI displays "
                    "(auto-resized when the physical buffer is noticeably "
                    "larger than the configured --screen WxH), so hid.click "
                    "coordinates derived from the screenshot are 1:1 with "
                    "click_point space; scale_x / scale_y will be ~1.0 on "
                    "macOS Retina after auto-resize. Capped at 4M output "
                    "pixels; oversized requests are silently downsampled "
                    "(see the raw_size field for what mss originally grabbed)."
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

    async def _converge_to_target(
        self, target_x: int, target_y: int, *, max_iters: int,
    ) -> dict[str, Any]:
        """Closed-loop settle to (target_x, target_y). Query OS cursor,
        emit residual delta, sleep one HID cycle, repeat until residual
        ≤ MOVE_TOLERANCE or max_iters exhausted.

        Returns:
            {"ok": True,  "x": actual, "y": actual, "target_x", "target_y",
             "iters": int, "converged": True}   on success / short-circuit;
            {"ok": False, "x": actual, "y": actual, "target_x", "target_y",
             "residual_x", "residual_y", "iters": max_iters,
             "converged": False, "hint": ...}   when residual stays > tol;
            {"error": ...}                       when OS cursor query fails.
        """
        landed: tuple[int, int] | None = None
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
                }
            await self.bridge.mouse_move(dx, dy, relative=True)
            landed = (cur[0] + dx, cur[1] + dy)
            if i < max_iters - 1:
                await asyncio.sleep(MOVE_SETTLE_MS / 1000.0)
        actual = get_cursor_position() or landed or (target_x, target_y)
        return {
            "ok": False,
            "x": actual[0], "y": actual[1],
            "target_x": target_x, "target_y": target_y,
            "residual_x": target_x - actual[0],
            "residual_y": target_y - actual[1],
            "iters": max_iters,
            "converged": False,
            "hint": (
                "cursor did not converge to target within tolerance; "
                "possible causes: competing input device (trackpad / "
                "physical mouse) active during the move, extreme "
                "pointer-acceleration settings, or a UI dead zone. "
                "Inspect actual (x, y) and decide whether to retry."
            ),
        }

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
        for i in range(1, steps + 1):
            t = i / steps
            target_dx = round(total_dx * t)
            target_dy = round(total_dy * t)
            step_dx = target_dx - accumulated_dx
            step_dy = target_dy - accumulated_dy
            if step_dx or step_dy:
                await self.bridge.mouse_move(step_dx, step_dy, relative=True)
            accumulated_dx = target_dx
            accumulated_dy = target_dy
            if i < steps:
                await asyncio.sleep(step_ms / 1000)
        # Slide done — closed-loop converge. 3 iters is enough here
        # because the slide already landed within tens of pixels;
        # snap-mode budgets 4 iters for cold-start moves up to ~1500 px.
        result = await self._converge_to_target(
            target_x, target_y, max_iters=MOVE_MAX_ITERS - 1,
        )
        if "error" in result:
            return result
        result["stepped"] = True
        result["steps"] = steps
        result["move_ms"] = move_ms
        return result

    async def _stepped_relative_move(
        self, dx: int, dy: int, move_ms: int,
    ) -> dict[str, Any]:
        """Same path stepping for ``relative=true`` callers — chunks
        the agent-supplied (dx, dy) into ~10 ms HID reports."""
        steps = self._plan_step_count(move_ms)
        step_ms = move_ms / steps
        accumulated_dx = 0
        accumulated_dy = 0
        for i in range(1, steps + 1):
            t = i / steps
            target_dx = round(dx * t)
            target_dy = round(dy * t)
            step_dx = target_dx - accumulated_dx
            step_dy = target_dy - accumulated_dy
            if step_dx or step_dy:
                await self.bridge.mouse_move(step_dx, step_dy, relative=True)
            accumulated_dx = target_dx
            accumulated_dy = target_dy
            if i < steps:
                await asyncio.sleep(step_ms / 1000)
        return {
            "dx": dx, "dy": dy,
            "stepped": True, "steps": steps, "move_ms": move_ms,
            "relative": True,
        }

    async def _tool_click(self, **kw) -> dict:
        self.rate.check()
        relative = bool(kw.get("relative", False))
        move_ms = max(0, min(MAX_MOVE_MS, int(kw.get("move_ms") or 0)))
        if relative:
            # Agent wants raw relative move — skip the cursor query.
            dx, dy = int(kw["x"]), int(kw["y"])
            if move_ms > 0:
                result = await self._stepped_relative_move(dx, dy, move_ms)
            else:
                await self.bridge.mouse_move(dx, dy, relative=True)
                result: dict[str, Any] = {"dx": dx, "dy": dy, "relative": True}
        else:
            if move_ms > 0:
                moved = await self._stepped_move_to_absolute(
                    kw["x"], kw["y"], move_ms,
                )
            else:
                moved = await self._move_to_absolute(kw["x"], kw["y"])
            if "error" in moved:
                return moved
            result = moved
        ok = await self.bridge.mouse_click(
            button=kw.get("button", "left"),
            double=bool(kw.get("double", False)),
        )
        result["ok"] = ok
        return result

    async def _tool_move(self, **kw) -> dict:
        self.rate.check()
        relative = bool(kw.get("relative", False))
        move_ms = max(0, min(MAX_MOVE_MS, int(kw.get("move_ms") or 0)))
        if relative:
            x, y = int(kw["x"]), int(kw["y"])
            if move_ms > 0:
                result = await self._stepped_relative_move(x, y, move_ms)
                result["ok"] = True
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
        if "error" in moved:
            return moved
        await asyncio.sleep(min(10_000, int(kw.get("duration_ms", 500))) / 1000.0)
        moved["ok"] = True
        return moved

    async def _tool_type(self, **kw) -> dict:
        self.rate.check()
        text = str(kw["text"])
        if len(text) > MAX_TYPE_LEN:
            raise ValueError(f"text too long ({len(text)} > {MAX_TYPE_LEN})")
        ok = await self.bridge.type_text(text)
        return {"ok": ok, "chars": len(text)}

    async def _tool_scroll(self, **kw) -> dict:
        self.rate.check()
        delta = int(kw["delta"])
        ok = await self.bridge.mouse_scroll(delta)
        return {"ok": ok, "delta": delta}

    async def _tool_key(self, **kw) -> dict:
        self.rate.check()
        key_str = str(kw["key"])
        modifiers = [m.lower() for m in (kw.get("modifiers") or [])]
        # Shortcut shorthand: "ctrl+c" / "ctrl+alt+l" — split modifiers
        # from the prefix when every "+"-separated head token is a known
        # modifier name. Keeps "+" itself usable as a literal key.
        if "+" in key_str and len(key_str) > 1:
            parts = key_str.split("+")
            head, tail = parts[:-1], parts[-1]
            if tail and all(p.lower() in _MODIFIER_NAMES for p in head):
                merged = list(dict.fromkeys(modifiers + [p.lower() for p in head]))
                modifiers = merged
                key_str = tail
        ok = await self.bridge.key_combo(modifiers, key_str)
        return {"ok": ok}

    async def _tool_release_all(self, **_kw) -> dict:
        ok = await self.bridge.release_all()
        return {"ok": ok}

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

    async def _tool_screenshot(self, **kw) -> "ImageResult":
        try:
            import mss  # type: ignore
            from PIL import Image  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "screenshot tool requires the '[screenshot]' extras: "
                "pip install 'clawtouch-mcp[screenshot]' "
                f"(missing: {e.name})"
            )
        fmt = (kw.get("format") or "jpeg").lower()
        if fmt not in ("jpeg", "png"):
            raise ValueError(
                f"format must be 'jpeg' or 'png', got {fmt!r}"
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
            img = Image.frombytes("RGB", (raw_w, raw_h), shot.rgb)

            # Step 1 — for full-screen captures, downsample to LOGICAL
            # resolution when the physical buffer is noticeably bigger
            # than the configured screen size. This is the Retina path
            # on macOS (~2x ratio) and Windows >100% DPI; on Linux /
            # 100% DPI the buffer matches screen_w/h and this is a
            # no-op. We use 1.2x as the threshold instead of !=1.0 to
            # tolerate a pixel of fractional scaling rounding noise.
            target_w, target_h = raw_w, raw_h
            if (not kw.get("region")
                    and self.config.screen_w and self.config.screen_h
                    and raw_w >= self.config.screen_w * 1.2):
                target_w = self.config.screen_w
                target_h = self.config.screen_h

            # Step 2 — even after the logical-resize step, the result
            # might still exceed MAX_OUTPUT_PIXELS (4K monitor at 1×,
            # or an absurdly large region). Ratio-downsample to fit.
            if target_w * target_h > MAX_OUTPUT_PIXELS:
                ratio = (MAX_OUTPUT_PIXELS / (target_w * target_h)) ** 0.5
                target_w = max(1, int(target_w * ratio))
                target_h = max(1, int(target_h * ratio))

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

            # After resize the screenshot pixel space matches the click
            # coordinate space, so scale_x / scale_y collapse to ~1.0
            # for full-screen captures. We still compute and emit them
            # so existing agent code that divides by scale stays correct
            # (it just divides by 1.0 and is unchanged).
            scale_x = 1.0
            scale_y = 1.0
            if (self.config.screen_w and self.config.screen_h
                    and not kw.get("region")):
                scale_x = target_w / self.config.screen_w
                scale_y = target_h / self.config.screen_h

        return ImageResult(
            image_bytes=image_bytes,
            mime_type=mime,
            metadata={
                "width": target_w,
                "height": target_h,
                "scale_x": round(scale_x, 4),
                "scale_y": round(scale_y, 4),
                "format": fmt,
                "mime_type": mime,
                "size_bytes": len(image_bytes),
                # Expose what mss originally grabbed so agents can
                # tell when an auto-resize happened. On Retina this
                # will be roughly 2x the (width, height) above.
                "raw_size": [raw_w, raw_h],
            },
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
        jid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
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
        writer.write(data + "\n")
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
