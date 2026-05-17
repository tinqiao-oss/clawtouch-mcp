"""Async serial bridge to a Pico 2 running the ClawTouch HID firmware.

Thin wrapper over pyserial; raw protocol only, no input-pacing or
timing logic at this layer. Higher layers (ClawTouch, OpenClaw, Hermes)
add their own scheduling.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

import serial  # type: ignore
import serial.tools.list_ports  # type: ignore

from .keycodes import (
    char_needs_shift,
    char_to_keycode,
    name_to_keycode,
)
from .protocol import (
    CommandType,
    FRAME_HEADER,
    HidCommand,
    ModifierKey,
    MouseButton,
    ProtocolError,
    build_key_combo,
    build_mouse_click,
    build_mouse_move,
    build_mouse_scroll,
    build_ping,
    build_type_string,
    modifiers_to_mask,
)

logger = logging.getLogger("clawtouch_mcp.bridge")


# ── Device discovery ──

# Raspberry Pi USB VID shared by all Pico generations. We only filter
# by VID — PID varies per board (Pico 0x0005, Pico W 0x000C, Pico 2
# 0x000B, …) and per firmware variant, so PID matching would be a
# constant maintenance tax. Boards from other vendors that happen to
# share this VID would also match, but in practice that does not occur.
_PICO_VID = 0x2E8A

# Match trailing digits in any port name (cu.usbmodem21203, ttyACM10, COM7)
# so we can sort the dual-CDC ports numerically rather than lexicographically
# — otherwise "COM10" < "COM3" lexicographically and we'd pick the wrong one.
_PORT_NUM_RE = re.compile(r"(\d+)$")


def _port_sort_key(device: str) -> tuple[int, str]:
    m = _PORT_NUM_RE.search(device or "")
    return (int(m.group(1)) if m else -1, device or "")


def list_pico_ports() -> list[dict[str, Any]]:
    """Return candidate serial ports that look like a Pico 2.

    The Pico firmware enables a composite USB device with TWO CDC channels:
    a REPL **console** (lower-numbered port) for firmware debug output,
    and a **data** channel (higher-numbered port) that speaks the framed
    HID protocol. Both ports share the same VID/PID/serial_number, so
    pyserial cannot tell them apart on its own.

    Each returned entry carries:
    - ``likely_pico``: ``True`` for both CDC ports of every Pico
    - ``is_data_port``: ``True`` only for the data channel — the one
      callers should actually open. Within each group of ports sharing
      a serial_number, the highest-numbered device wins (Apple / Linux /
      Windows convention: interface declaration order → port number).

    Single-CDC firmwares (or boards with only one port enumerated)
    have ``is_data_port=True`` on their sole port — the heuristic
    degrades gracefully.
    """
    raw: list[dict[str, Any]] = []
    for p in serial.tools.list_ports.comports():
        entry = {
            "device": p.device,
            "name": p.name,
            "description": p.description,
            "vid": p.vid,
            "pid": p.pid,
            "serial_number": p.serial_number,
            "manufacturer": p.manufacturer,
            "likely_pico": (p.vid == _PICO_VID and p.pid is not None),
            "is_data_port": False,
        }
        raw.append(entry)

    # Group likely-Pico entries by serial_number; within each group, the
    # highest-numbered port is the data channel. Empty serial groups all
    # serial-less devices together — rare in practice, but the highest-
    # numbered one is still a safer pick than the first.
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in raw:
        if not e["likely_pico"]:
            continue
        groups.setdefault(e["serial_number"] or "", []).append(e)
    for ports in groups.values():
        ports.sort(key=lambda x: _port_sort_key(x["device"]))
        ports[-1]["is_data_port"] = True

    return raw


def auto_detect_port() -> Optional[str]:
    """Return the data-channel port of the first detected Pico, or None.

    Prefers the explicit data port (correct for dual-CDC composite devices).
    Falls back to any likely-Pico port if no group has a data port marked —
    shouldn't happen with the current ``list_pico_ports`` logic but keeps
    the function defensive against future schema changes.
    """
    ports = list_pico_ports()
    for p in ports:
        if p["is_data_port"]:
            return p["device"]
    for p in ports:  # defensive fallback
        if p["likely_pico"]:
            return p["device"]
    return None


def auto_detect_ports() -> list[str]:
    """Return all detected Pico data-channel ports, ordered.

    Same logic as ``auto_detect_port`` but returns the full list, letting
    callers fall back across multiple boards when the first one is busy
    (e.g. occupied by another process such as ClawTouch on the same machine).
    Data ports come first, then defensive likely-Pico ports.
    """
    ports = list_pico_ports()
    out: list[str] = []
    for p in ports:
        if p["is_data_port"]:
            out.append(p["device"])
    for p in ports:
        if p["likely_pico"] and p["device"] not in out:
            out.append(p["device"])
    return out


# ── Bridge ──

class SerialHidBridge:
    """Async serial HID bridge.

    Thread-safe for a single event-loop consumer; commands are serialized
    through an asyncio.Lock. Reads are blocking on a background executor.
    """

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: Optional[serial.Serial] = None
        self._seq = 0
        self._lock = asyncio.Lock()
        self._connected_at: Optional[float] = None

    # ── Lifecycle ──

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    async def connect(self) -> None:
        if self.is_connected:
            return
        loop = asyncio.get_running_loop()
        self._serial = await loop.run_in_executor(
            None,
            lambda: serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=self.timeout,
            ),
        )
        self._connected_at = time.time()
        # Drain any residual bytes
        await asyncio.sleep(0.1)
        if self._serial.in_waiting:
            self._serial.reset_input_buffer()
        logger.info("connected to %s @ %d baud", self.port, self.baudrate)

    async def close(self) -> None:
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
        self._serial = None
        self._connected_at = None

    # ── Low-level IO ──

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    async def _send_raw(self, cmd: HidCommand, *, wait_ack: bool = True) -> Optional[HidCommand]:
        if not self.is_connected:
            raise ConnectionError("bridge is not connected")
        assert self._serial is not None
        loop = asyncio.get_running_loop()
        data = cmd.serialize()
        async with self._lock:
            await loop.run_in_executor(None, self._serial.write, data)
            await loop.run_in_executor(None, self._serial.flush)
            if not wait_ack:
                return None
            return await loop.run_in_executor(None, self._read_one_frame)

    def _read_one_frame(self) -> Optional[HidCommand]:
        """Blocking read: sync on header, parse one frame."""
        assert self._serial is not None
        deadline = time.monotonic() + self.timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            b = self._serial.read(1)
            if not b:
                continue
            if b[0] == FRAME_HEADER:
                buf = bytearray(b)
                break
        if not buf:
            return None
        # Need: 2 seq + 1 cmd + 2 plen = 5 more bytes
        remaining = self._serial.read(5)
        if len(remaining) < 5:
            return None
        buf.extend(remaining)
        plen = int.from_bytes(buf[4:6], "little")
        rest = self._serial.read(plen + 1)  # payload + checksum
        if len(rest) < plen + 1:
            return None
        buf.extend(rest)
        try:
            return HidCommand.deserialize(bytes(buf))
        except ProtocolError as e:
            logger.warning("frame parse error: %s", e)
            return None

    # ── High-level commands ──

    async def ping(self) -> bool:
        resp = await self._send_raw(build_ping(self._next_seq()))
        return resp is not None and resp.cmd_type in (CommandType.PONG, CommandType.ACK)

    async def mouse_move(self, x: int, y: int, *, relative: bool = False) -> bool:
        resp = await self._send_raw(
            build_mouse_move(x, y, relative=relative, seq_id=self._next_seq())
        )
        return resp is not None and resp.cmd_type == CommandType.ACK

    async def mouse_click(self, button: str = "left", *, double: bool = False) -> bool:
        btn = {
            "left": MouseButton.LEFT,
            "right": MouseButton.RIGHT,
            "middle": MouseButton.MIDDLE,
        }.get(button.lower())
        if btn is None:
            raise ValueError(f"unknown button: {button}")
        resp = await self._send_raw(
            build_mouse_click(btn, double=double, seq_id=self._next_seq())
        )
        return resp is not None and resp.cmd_type == CommandType.ACK

    async def mouse_scroll(self, delta: int) -> bool:
        resp = await self._send_raw(
            build_mouse_scroll(delta, seq_id=self._next_seq())
        )
        return resp is not None and resp.cmd_type == CommandType.ACK

    async def type_text(self, text: str, *, chunk_size: int = 32) -> bool:
        """Send text in UTF-8 chunks; firmware handles keyboard emulation."""
        ok = True
        for i in range(0, len(text), chunk_size):
            resp = await self._send_raw(
                build_type_string(text[i:i + chunk_size], seq_id=self._next_seq())
            )
            ok = ok and (resp is not None and resp.cmd_type == CommandType.ACK)
        return ok

    async def key_combo(self, modifiers: list[str], key: str) -> bool:
        mask = modifiers_to_mask(modifiers)
        kc = name_to_keycode(key)
        if kc is None and len(key) == 1:
            if char_needs_shift(key):
                mask |= int(ModifierKey.SHIFT)
            kc = char_to_keycode(key)
        if kc is None:
            raise ValueError(f"unknown key: {key!r}")
        resp = await self._send_raw(
            build_key_combo(mask, kc, seq_id=self._next_seq())
        )
        return resp is not None and resp.cmd_type == CommandType.ACK

    async def release_all(self) -> bool:
        """Force release all keys/buttons: send KEY_RELEASE with no payload."""
        from .protocol import build_key_release
        resp = await self._send_raw(build_key_release(seq_id=self._next_seq()))
        return resp is not None and resp.cmd_type == CommandType.ACK

    # ── Introspection ──

    def device_info(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "baudrate": self.baudrate,
            "connected": self.is_connected,
            "connected_at": self._connected_at,
            "seq": self._seq,
        }
