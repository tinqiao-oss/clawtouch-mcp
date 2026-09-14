# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Async serial bridge to a Pico 2 running the ClawTouch HID firmware.

Thin wrapper over pyserial; raw protocol only — no agent-loop logic
at this layer. Higher layers (ClawTouch, OpenClaw, Hermes) add their
own scheduling and orchestration.
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
    name_needs_shift,
    name_to_keycode,
)
from .protocol import (
    CommandType,
    ErrorCode,
    FRAME_HEADER,
    HidCommand,
    ModifierKey,
    MouseButton,
    ProtocolError,
    build_key_combo,
    build_mouse_button_down,
    build_mouse_button_up,
    build_mouse_click,
    build_mouse_move,
    build_mouse_scroll,
    build_ping,
    build_type_string,
    modifiers_to_mask,
)


# ── Bridge exceptions / diagnostics ──

class BridgeError(RuntimeError):
    """Base class for `SerialHidBridge` IO / protocol failures.

    Distinct from generic `RuntimeError` so server-side tool handlers
    can surface a clear, actionable message to the agent instead of a
    bare ``ok: false``. Server returns these as ``isError: true``
    content blocks per MCP spec.
    """


class BridgeAckTimeout(BridgeError):
    """No frame at all came back inside the bridge's timeout window."""


class BridgeAckMismatch(BridgeError):
    """A frame came back but its seq_id did not match the request — the
    line may be carrying a stale ACK from a prior aborted request.
    """


class BridgeProtocolError(BridgeError):
    """A frame came back but failed parse (bad checksum, short payload)."""


class BridgeErrorResponse(BridgeError):
    """The firmware returned an ERROR frame instead of an ACK.

    ``code`` is the parsed ``ErrorCode`` (UNKNOWN_COMMAND /
    INVALID_PAYLOAD / CHECKSUM_MISMATCH / EXECUTION_TIMEOUT /
    DEVICE_BUSY); ``detail`` is the firmware's optional text suffix.
    """

    def __init__(self, code: "ErrorCode | int", detail: str = ""):
        self.code = code
        self.detail = detail
        name = code.name if hasattr(code, "name") else f"0x{int(code):02x}"
        super().__init__(f"firmware error {name}: {detail}" if detail else f"firmware error {name}")

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


def _location_parts(location: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    """Split a pyserial ``location`` into (USB device path, interface number).

    Windows reports ``1-13:x.2`` and Linux ``1-1.4:1.2`` — the device's place
    in the USB tree, then (after the last ``:``) the interface, whose number is
    the last dot-separated part. macOS reports only the device's place
    (``20-1.4``, no ``:``): that is the path, with no interface number — its
    device names already encode the interface (``usbmodem21201`` /
    ``…21203``). Either part is None when it cannot be read.
    """
    if not location:
        return None, None
    if ":" not in location:
        return location, None
    device_path, _, tail = location.rpartition(":")
    last = tail.rsplit(".", 1)[-1]
    interface = int(last) if last.isascii() and last.isdigit() else None
    return (device_path or None), interface


def untypable_chars(text: str) -> list[str]:
    """Distinct characters in ``text`` that the firmware's US keyboard layout
    has no key for, in first-seen order. Printable ASCII is typeable; the C0
    controls and DEL are stripped before sending (``type_text``), so only
    code points above 0x7F are reported."""
    return list(dict.fromkeys(ch for ch in text if ord(ch) > 0x7F))


def check_typeable(text: str) -> None:
    """Refuse text the device cannot type, before any of it is sent.

    The firmware presses one key per character on a US layout and stops at
    the first character without a key — after typing everything before it.
    Checking first turns a half-typed field into a refusal with nothing
    typed. Raises ValueError (which the MCP server returns as ``isError``)."""
    bad = untypable_chars(text)
    if not bad:
        return
    shown = " ".join(repr(c) for c in bad[:8])
    if len(bad) > 8:
        shown += f" and {len(bad) - 8} more"
    raise ValueError(
        f"nothing was typed: the text contains {shown}, which the device's "
        "US keyboard layout has no key for. It types one key per character, "
        "so it would stop at the first of them with the text half entered. "
        "Rewrite those in plain ASCII, or put the text on the clipboard and "
        "paste it (hid.key ctrl+v, or cmd+v on macOS)."
    )


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
      callers should actually open. One port per board: the ports of a
      board are grouped by serial number (or, for boards that report none,
      by their place in the USB tree, when every such port reports one),
      and within a group the port on the highest USB **interface number**
      is the data channel — the console is declared first. Where the OS
      reports no interface number for every port of the group (macOS), the
      highest-numbered device name stands in for it, which on macOS encodes
      the same thing.
    - ``location``: pyserial's location string, as reported.

    Single-CDC firmwares (or boards with only one port enumerated)
    have ``is_data_port=True`` on their sole port — the heuristic
    degrades gracefully.
    """
    raw: list[dict[str, Any]] = []
    for p in serial.tools.list_ports.comports():
        location = getattr(p, "location", None)
        entry = {
            "device": p.device,
            "name": p.name,
            "description": p.description,
            "vid": p.vid,
            "pid": p.pid,
            "serial_number": p.serial_number,
            "manufacturer": p.manufacturer,
            "location": location,
            "likely_pico": (p.vid == _PICO_VID and p.pid is not None),
            "is_data_port": False,
        }
        raw.append(entry)

    # One group per board. The serial number identifies a board, and a port
    # that reports none joins the board of any port at the same USB device
    # path that does. Boards without any serial number are told apart by
    # their USB path — but only when every such port has one: a port whose
    # path could not be read cannot be matched to its board, and splitting
    # its board would leave a lone console marked as a data port. Then all of
    # them share one group, as every serial-less port did before — one data
    # port for all of them is safer than offering ports that may be consoles.
    picos = [e for e in raw if e["likely_pico"]]
    path_of = {id(e): _location_parts(e["location"])[0] for e in picos}
    serial_at: dict[str, str] = {}
    for e in picos:
        if e["serial_number"] and path_of[id(e)]:
            serial_at.setdefault(path_of[id(e)], e["serial_number"])

    def serial_for(e: dict[str, Any]) -> Optional[str]:
        path = path_of[id(e)]
        return e["serial_number"] or (serial_at.get(path) if path else None)

    split_by_path = all(path_of[id(e)] for e in picos if not serial_for(e))
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in picos:
        board_serial = serial_for(e)
        if board_serial:
            key = f"sn:{board_serial}"
        elif split_by_path:
            key = f"usb:{path_of[id(e)]}"
        else:
            key = ""
        groups.setdefault(key, []).append(e)
    for ports in groups.values():
        interfaces = [_location_parts(x["location"])[1] for x in ports]
        if all(i is not None for i in interfaces):
            # The interface number says which channel a port is. Port
            # numbers do not: Windows hands out COM numbers from whatever is
            # free, so a console can end up above its data port.
            ports.sort(key=lambda x: _location_parts(x["location"])[1])
        else:
            ports.sort(key=lambda x: _port_sort_key(x["device"]))
        ports[-1]["is_data_port"] = True

    return raw


def auto_detect_port() -> Optional[str]:
    """Return the data-channel port of the first detected Pico, or None."""
    ports = auto_detect_ports()
    return ports[0] if ports else None


def auto_detect_ports() -> list[str]:
    """Return the data-channel port of every detected Pico, in enumeration
    order — one port per board.

    The full list lets callers fall back across several boards when the
    first one is busy (e.g. held by another process such as ClawTouch on the
    same machine). A port that ``list_pico_ports`` did not mark as a data
    port is never offered, not even as a fallback when the data port is busy.
    That fallback used to exist, and with one board it meant: data port held
    by another program → connect to the same board's console instead and
    report "connected". The console is CircuitPython's REPL, which reads what
    arrives as keystrokes, and a protocol frame carries its sequence number
    in plain bytes — seq 3 and 4 are 0x03 / 0x04, Ctrl-C and Ctrl-D, which
    interrupt and restart the firmware the other program is using.
    """
    return [p["device"] for p in list_pico_ports() if p["is_data_port"]]


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
        # Seq counter starts at 0 so the first `_next_seq()` returns 1.
        # We deliberately skip 0 on wrap (see `_next_seq`) so a stale
        # in-flight default-seq frame can never collide with a fresh
        # request after the 16-bit counter wraps around.
        self._seq = 0
        self._lock = asyncio.Lock()
        self._connected_at: Optional[float] = None
        # Diagnostic for the most recent failed _send_raw — server-side
        # tool handlers read this to enrich their isError content.
        self._last_error_detail: Optional[str] = None
        # Progress of the most recent type_text (see there).
        self.last_type_confirmed = 0
        self.last_type_unconfirmed = 0

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
        # Reset seq counter on every (re)connect. A physical USB unplug
        # then replug means the firmware end resets its own seq state
        # too — if the host kept its counter advancing, the first request
        # after reconnect could carry a high seq while firmware is back
        # near zero, raising the chance of stale-ACK ambiguity. round 4
        # added wrap-skip-0 + seq_id verify on the wire; this resets the
        # source so the wire-level defenses see a clean counter.
        self._seq = 0
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
        # 16-bit counter. After 0xFFFF wraps to 0 — skip 0 so a stale
        # frame built with the protocol's default ``seq_id=0`` can
        # never be mistaken for the response to a real request.
        nxt = (self._seq + 1) & 0xFFFF
        if nxt == 0:
            nxt = 1
        self._seq = nxt
        return self._seq

    @property
    def last_error_detail(self) -> Optional[str]:
        """Human-readable reason for the most recent ``_send_raw``
        failure (timeout / seq mismatch / parse error / firmware ERROR
        response). Cleared on the next successful send."""
        return self._last_error_detail

    async def _send_raw(self, cmd: HidCommand, *, wait_ack: bool = True) -> Optional[HidCommand]:
        if not self.is_connected:
            raise ConnectionError("bridge is not connected")
        assert self._serial is not None
        loop = asyncio.get_running_loop()
        data = cmd.serialize()
        async with self._lock:
            # Reset pyserial's input buffer BEFORE writing so any stale
            # bytes left over from an aborted prior request (timeout,
            # parser desync) cannot be mistaken for this request's ACK.
            # Without this, a stray 0xAA byte in payload from the old
            # request can re-sync the parser onto mid-frame data and
            # surface as a "frame parse error" or — worse — a stale
            # ACK matched to the new request's seq_id.
            try:
                self._serial.reset_input_buffer()
            except Exception as e:
                logger.warning("reset_input_buffer failed: %s", e)

            await loop.run_in_executor(None, self._serial.write, data)
            await loop.run_in_executor(None, self._serial.flush)
            if not wait_ack:
                self._last_error_detail = None
                return None
            resp = await loop.run_in_executor(None, self._read_one_frame)
            if resp is None:
                # `_read_one_frame` already set `_last_error_detail`
                # with the specific reason (timeout / short / parse).
                return None
            # ACK seq_id must match the request's. Mismatch = the
            # response we just read belongs to a previous, abandoned
            # request — the line is desynchronised and we should NOT
            # treat this as success.
            if resp.seq_id != cmd.seq_id:
                self._last_error_detail = (
                    f"seq mismatch: request seq={cmd.seq_id} "
                    f"got seq={resp.seq_id} (stale ACK from prior request?)"
                )
                logger.warning(self._last_error_detail)
                return None
            # Firmware can answer ERROR (cmd_type=0xFF) carrying an
            # ErrorCode in payload[0] — surface that to callers via
            # last_error_detail so they don't see a generic "ok=False".
            if resp.cmd_type == CommandType.ERROR and resp.payload:
                try:
                    code = ErrorCode(resp.payload[0])
                    code_name = code.name
                except (ValueError, IndexError):
                    code_name = f"0x{resp.payload[0]:02x}"
                tail = resp.payload[1:].decode("ascii", errors="replace") if len(resp.payload) > 1 else ""
                self._last_error_detail = (
                    f"firmware ERROR {code_name}" + (f": {tail}" if tail else "")
                )
                # Return the response so callers can still test
                # `resp.cmd_type == ACK`, which will correctly be False.
                return resp
            self._last_error_detail = None
            return resp

    def _read_one_frame(self) -> Optional[HidCommand]:
        """Blocking read: sync on header, parse one frame.

        Sets ``self._last_error_detail`` on every failure path so
        ``_send_raw`` can surface a specific reason to callers (which
        the server then forwards to the agent as ``isError`` content).
        """
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
            self._last_error_detail = (
                f"ACK timeout after {self.timeout:.1f}s "
                "(no frame header received from firmware)"
            )
            return None
        # Need: 2 seq + 1 cmd + 2 plen = 5 more bytes
        remaining = self._serial.read(5)
        if len(remaining) < 5:
            self._last_error_detail = (
                f"truncated frame: got header but only {len(remaining)}/5 "
                "preamble bytes before timeout"
            )
            return None
        buf.extend(remaining)
        plen = int.from_bytes(buf[4:6], "little")
        rest = self._serial.read(plen + 1)  # payload + checksum
        if len(rest) < plen + 1:
            self._last_error_detail = (
                f"truncated frame: plen={plen} but only got "
                f"{len(rest)}/{plen + 1} payload+csum bytes"
            )
            return None
        buf.extend(rest)
        try:
            return HidCommand.deserialize(bytes(buf))
        except ProtocolError as e:
            self._last_error_detail = f"frame parse error: {e}"
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

    @staticmethod
    def _resolve_button(button: str) -> MouseButton:
        btn = {
            "left": MouseButton.LEFT,
            "right": MouseButton.RIGHT,
            "middle": MouseButton.MIDDLE,
        }.get(button.lower())
        if btn is None:
            raise ValueError(f"unknown button: {button}")
        return btn

    async def mouse_button_down(self, button: str = "left") -> bool:
        """v1.1: press a mouse button without releasing it. Use with
        ``mouse_button_up`` (and ``mouse_move`` in between) to compose
        a drag gesture, or call ``release_all`` to panic-stop.
        """
        resp = await self._send_raw(
            build_mouse_button_down(self._resolve_button(button), seq_id=self._next_seq())
        )
        return resp is not None and resp.cmd_type == CommandType.ACK

    async def mouse_button_up(self, button: str = "left") -> bool:
        """v1.1: release a previously-pressed mouse button. Idempotent."""
        resp = await self._send_raw(
            build_mouse_button_up(self._resolve_button(button), seq_id=self._next_seq())
        )
        return resp is not None and resp.cmd_type == CommandType.ACK

    async def type_text(
        self, text: str, *, chunk_size: int = 32,
        allow_control: bool = False,
    ) -> bool:
        """Send text in UTF-8 chunks; firmware handles keyboard emulation.

        Control characters (``\\n``, ``\\r``, ``\\t``, ``\\x00``-``\\x1f``)
        are stripped by default. An LLM agent drafting a multi-line
        message into a chat input would otherwise have its draft
        accidentally submitted by the ``\\n`` being typed as Enter on
        the host. Pass ``allow_control=True`` to opt in to the raw
        behaviour (e.g. when intentionally driving a terminal app).

        Text containing a character the US layout cannot type raises
        ValueError before anything is sent (see ``check_typeable``).
        """
        check_typeable(text)
        if not allow_control:
            # Drop control bytes (incl. \n, \r, \t, NUL). Tab is in
            # the printable HID set but typing it into a chat input
            # has the same "accidental submit" risk as newline in
            # some apps, so we strip it too.
            cleaned = "".join(
                ch for ch in text
                if not (ch < " " or ch == "\x7f")
            )
            if cleaned != text:
                logger.info(
                    "type_text stripped %d control character(s); "
                    "pass allow_control=True to keep them",
                    len(text) - len(cleaned),
                )
            text = cleaned
        # How far this call got, for a caller reporting a failure: characters
        # in chunks the device acknowledged, and the size of the chunk that
        # failed (some of which may have been typed).
        self.last_type_confirmed = 0
        self.last_type_unconfirmed = 0
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i + chunk_size]
            resp = await self._send_raw(
                build_type_string(chunk, seq_id=self._next_seq())
            )
            if resp is None or resp.cmd_type != CommandType.ACK:
                # Stop at the first chunk that was not acknowledged. The
                # firmware types a chunk key by key and may have typed part
                # of it before failing; sending the chunks after it would
                # type the rest of the text around a gap — output that looks
                # finished in the target field and is not.
                self.last_type_unconfirmed = len(chunk)
                return False
            self.last_type_confirmed += len(chunk)
        return True

    async def key_combo(self, modifiers: list[str], key: str) -> bool:
        mask = modifiers_to_mask(modifiers)
        kc = name_to_keycode(key)
        if kc is not None:
            # Named key — OR SHIFT in when the name refers to the
            # SHIFTED glyph (plus / tilde / quote). Without this,
            # `hid.key("plus")` would emit `=` because the underlying
            # HID keycode 0x2E is the un-shifted key for `=` / `+`.
            if name_needs_shift(key):
                mask |= int(ModifierKey.SHIFT)
        elif len(key) == 1:
            # Single-char fallback (e.g. agent passed "ctrl+a" or
            # "ctrl++"). Look the char up in the printable-glyph
            # tables and force SHIFT for shifted glyphs.
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
        """Force release all keys/buttons: send KEY_RELEASE with
        keycode=0 / modifiers=0 (panic-stop semantics — firmware
        releases every held key and mouse button)."""
        from .protocol import build_key_release
        resp = await self._send_raw(build_key_release(seq_id=self._next_seq()))
        return resp is not None and resp.cmd_type == CommandType.ACK

    def _resolve_key(self, key: str, modifiers: list[str] | None) -> tuple[int, int]:
        """Translate (key_name_or_char, modifier_names) to (keycode, modifier_mask).

        Same resolution path as :meth:`key_combo` — named keys first
        (Enter / F1 / Tab / etc.), then single-char fallback with
        auto-SHIFT for shifted glyphs. Raises ValueError on unknown key.
        """
        mask = modifiers_to_mask(modifiers or [])
        kc = name_to_keycode(key)
        if kc is not None:
            if name_needs_shift(key):
                mask |= int(ModifierKey.SHIFT)
        elif len(key) == 1:
            if char_needs_shift(key):
                mask |= int(ModifierKey.SHIFT)
            kc = char_to_keycode(key)
        if kc is None:
            raise ValueError(f"unknown key: {key!r}")
        return kc, mask

    async def key_press(self, key: str, modifiers: list[str] | None = None) -> bool:
        """v1.1 exposure: press a key (or shortcut) without releasing. Pair
        with :meth:`key_release`, or use ``hid.hold_key`` tool for a
        duration wrapper. Useful for 'hold shift while clicking N times'
        style multi-select patterns."""
        from .protocol import build_key_press
        kc, mask = self._resolve_key(key, modifiers)
        resp = await self._send_raw(
            build_key_press(kc, mask, seq_id=self._next_seq())
        )
        return resp is not None and resp.cmd_type == CommandType.ACK

    async def key_release(self, key: str = "", modifiers: list[str] | None = None) -> bool:
        """v1.1 exposure: release a previously-pressed key. Empty key
        (default) sends the all-zero KEY_RELEASE which firmware treats
        as release-all (panic-stop for both keys and mouse buttons)."""
        from .protocol import build_key_release
        if not key:
            resp = await self._send_raw(build_key_release(seq_id=self._next_seq()))
        else:
            kc, mask = self._resolve_key(key, modifiers)
            resp = await self._send_raw(
                build_key_release(kc, mask, seq_id=self._next_seq())
            )
        return resp is not None and resp.cmd_type == CommandType.ACK

    # ── Introspection ──

    async def device_info(self) -> dict[str, Any]:
        """Snapshot of the bridge's current connection state.

        Pure metadata — no I/O — but declared ``async`` so it matches
        the call style of every other public method on the three
        bridge classes (``connect`` / ``close`` / ``ping`` /
        ``mouse_move`` / ``type_text`` / ``release_all`` / ...).
        Users who reach for ``await b.device_info()`` based on the
        rest of the surface get the expected dict; users who guess
        it's sync get a clear "coroutine was never awaited" warning
        rather than a silent foot-gun.
        """
        return {
            "port": self.port,
            "baudrate": self.baudrate,
            "connected": self.is_connected,
            "connected_at": self._connected_at,
            "seq": self._seq,
        }
