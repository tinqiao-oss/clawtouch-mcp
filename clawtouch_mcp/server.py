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
from .cursor import availability_hint, get_cursor_position

logger = logging.getLogger("clawtouch_mcp.server")

MCP_PROTOCOL_VERSION = "2024-11-05"
MAX_TYPE_LEN = 4096
_MODIFIER_NAMES = frozenset({"ctrl", "shift", "alt", "gui", "win", "cmd"})


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
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
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
    """In-memory bridge for `--mock` mode; logs but never touches hardware."""

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

    def device_info(self) -> dict:
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

    def device_info(self) -> dict:
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
        """周期检查 idle 阈值. 触发 release 后退出, 下次 tool call 重新启动."""
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
                    await self._idle_release_now()
                    return  # release 后 bridge 是 UnavailableBridge, 退出
        except asyncio.CancelledError:
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
                "relative=true."
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
                "works under the hood). Pass relative=true to send "
                "(x, y) as a pixel delta directly. On hosts where the "
                "OS cursor query is unavailable, absolute mode returns "
                "an error."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "relative": {"type": "boolean", "default": False},
                },
                "required": ["x", "y"],
            },
            handler=self._tool_move,
        ))
        self._register(Tool(
            name="hid.hover",
            description="Move mouse to (x,y) then idle for duration_ms (no click).",
            input_schema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "duration_ms": {"type": "integer", "default": 500, "minimum": 0, "maximum": 10000},
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
                description="Take a screenshot (requires --allow-screenshot + mss).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "region": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 4, "maxItems": 4,
                            "description": "[x1,y1,x2,y2]",
                        }
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

    async def _move_to_absolute(self, target_x: int, target_y: int) -> dict[str, Any]:
        """Shared helper for click/move/hover: clamp, translate to
        delta via OS cursor query, send relative move. Returns a
        result dict; if ``"error"`` is in the dict the caller should
        return it as the tool error without continuing."""
        target_x, target_y = self._clamp(target_x, target_y)
        delta = self._absolute_to_relative(target_x, target_y)
        if delta is None:
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
        dx, dy = delta
        ok = await self.bridge.mouse_move(dx, dy, relative=True)
        return {"ok": ok, "x": target_x, "y": target_y, "dx": dx, "dy": dy}

    async def _tool_click(self, **kw) -> dict:
        self.rate.check()
        relative = bool(kw.get("relative", False))
        if relative:
            # Agent wants raw relative move — skip the cursor query.
            dx, dy = int(kw["x"]), int(kw["y"])
            await self.bridge.mouse_move(dx, dy, relative=True)
            result: dict[str, Any] = {"dx": dx, "dy": dy, "relative": True}
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
        if relative:
            x, y = int(kw["x"]), int(kw["y"])
            ok = await self.bridge.mouse_move(x, y, relative=True)
            return {"ok": ok, "x": x, "y": y, "relative": True}
        moved = await self._move_to_absolute(kw["x"], kw["y"])
        if "error" in moved:
            return moved
        moved["relative"] = False
        return moved

    async def _tool_hover(self, **kw) -> dict:
        self.rate.check()
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
        info = self.bridge.device_info() if self.bridge else {}
        return {
            "info": info,
            "screen": {
                "width": self.config.screen_w,
                "height": self.config.screen_h,
                "source": self._screen_source,
            },
            "mcp_version": __version__,
        }

    async def _tool_screenshot(self, **kw) -> dict:
        try:
            import mss  # type: ignore
            import base64
        except ImportError:
            raise RuntimeError(
                "screenshot tool requires the `mss` extra: "
                "pip install 'clawtouch-mcp[screenshot]'"
            )
        with mss.MSS() as sct:  # `mss.mss()` deprecated in mss 10.x
            if "region" in kw:
                x1, y1, x2, y2 = kw["region"]
                if x2 <= x1 or y2 <= y1:
                    raise ValueError(
                        f"invalid region {kw['region']}: "
                        "need x2 > x1 and y2 > y1"
                    )
                monitor = {"left": int(x1), "top": int(y1),
                           "width": int(x2 - x1), "height": int(y2 - y1)}
            else:
                monitor = sct.monitors[1]
            shot = sct.grab(monitor)
            png = mss.tools.to_png(shot.rgb, shot.size)
        return {
            "mime_type": "image/png",
            "width": shot.width,
            "height": shot.height,
            "base64": base64.b64encode(png).decode("ascii"),
        }

    # ═══════════════════ JSON-RPC dispatch ═══════════════════

    async def dispatch(self, msg: dict) -> Optional[dict]:
        """Return a response dict, or None for notifications."""
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
                result = {}
            else:
                return _error_response(jid, -32601, f"method not found: {method}")
            if jid is None:
                return None  # notification — no response
            return {"jsonrpc": "2.0", "id": jid, "result": result}
        except Exception as e:
            logger.exception("dispatch error for %s", method)
            return _error_response(jid, -32000, str(e))

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
            raise ValueError(f"unknown tool: {name!r}")
        result = await tool.handler(**args)
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


async def run_stdio(server: ClawTouchMcpServer) -> None:
    """Main loop: read stdin, dispatch, write stdout."""
    # Decide framing on first message
    framed: Optional[bool] = None
    try:
        while True:
            raw = await _read_line()
            if not raw:
                return
            first = raw.decode("utf-8", errors="replace").strip()
            if not first:
                continue
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

            resp = await server.dispatch(msg)
            if resp is not None:
                _write_message(sys.stdout, resp, framed=framed)

            # Subsequent messages use the same framing
            while True:
                msg = await _read_one(framed)
                if msg is None:
                    return
                resp = await server.dispatch(msg)
                if resp is not None:
                    _write_message(sys.stdout, resp, framed=framed)
    except asyncio.IncompleteReadError:
        return
    except Exception:
        logger.exception("stdio loop crashed")
        raise
