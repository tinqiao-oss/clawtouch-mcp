# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Port detection — dual-CDC composite device handling.

The Pico firmware exposes TWO CDC channels (console + data) that
share the same VID/PID/serial_number. Pre-0.2.1 logic returned the
first match, which on macOS/Linux/Windows is the console channel
(REPL) — silently breaking every protocol PING because the REPL
echoes bytes back instead of executing the framed protocol.

These tests lock the correct behavior: ``is_data_port=True`` is set
only on the highest-numbered port within each shared-serial group,
and ``auto_detect_port()`` returns that port.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch

import pytest

from clawtouch_mcp.bridge import (
    _PICO_VID,
    _port_sort_key,
    auto_detect_port,
    auto_detect_ports,
    list_pico_ports,
)
from clawtouch_mcp.server import ClawTouchMcpServer, ServerConfig, UnavailableBridge


@dataclass
class FakePort:
    """Mimics enough of pyserial's `ListPortInfo` for our detection logic."""
    device: str
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial_number: Optional[str] = None
    name: Optional[str] = None
    description: str = "n/a"
    manufacturer: Optional[str] = None
    location: Optional[str] = None


def _mock_comports(ports):
    return patch("serial.tools.list_ports.comports", return_value=ports)


class TestSortKey:
    """Natural numeric sort over port names — avoids 'COM10' < 'COM3' trap."""

    def test_macos_consecutive_cdc(self):
        # Apple convention: lower-numbered = console, higher = data
        k1 = _port_sort_key("/dev/cu.usbmodem21201")
        k2 = _port_sort_key("/dev/cu.usbmodem21203")
        assert k1 < k2

    def test_windows_double_digit_com_natural_order(self):
        # COM10 must sort AFTER COM3 numerically (lexicographic would invert)
        assert _port_sort_key("COM3") < _port_sort_key("COM10")

    def test_linux_ttyacm(self):
        assert _port_sort_key("/dev/ttyACM0") < _port_sort_key("/dev/ttyACM1")

    def test_no_trailing_digit_falls_back(self):
        # If a device name has no trailing digits we put it first (lowest)
        # so a real numbered device wins.
        k_named = _port_sort_key("/dev/something")
        k_numbered = _port_sort_key("/dev/cu.usbmodem21203")
        assert k_named < k_numbered


class TestDualCdcDetection:
    """The real bug: two ports same serial → highest-numbered is data."""

    def test_macos_dual_cdc_picks_higher_numbered_port(self):
        """macOS exposes Pico's two USB-CDC interfaces as paired ports
        with identical serial numbers; the higher-numbered one is the
        data channel (lower is the REPL console)."""
        with _mock_comports([
            FakePort(device="/dev/cu.usbmodem21201", vid=_PICO_VID, pid=11,
                     serial_number="E660000000000000"),
            FakePort(device="/dev/cu.usbmodem21203", vid=_PICO_VID, pid=11,
                     serial_number="E660000000000000"),
        ]):
            ports = list_pico_ports()
            console = next(p for p in ports if "21201" in p["device"])
            data = next(p for p in ports if "21203" in p["device"])
            assert console["likely_pico"] is True
            assert console["is_data_port"] is False, "21201 is REPL not data"
            assert data["likely_pico"] is True
            assert data["is_data_port"] is True, "21203 is the data channel"
            assert auto_detect_port() == "/dev/cu.usbmodem21203"

    def test_windows_dual_com_with_two_digit_number(self):
        """COM3 + COM10 → COM10 wins (natural sort, not lexicographic)."""
        with _mock_comports([
            FakePort(device="COM10", vid=_PICO_VID, pid=11, serial_number="ABC"),
            FakePort(device="COM3", vid=_PICO_VID, pid=11, serial_number="ABC"),
        ]):
            assert auto_detect_port() == "COM10"

    def test_linux_ttyacm_dual(self):
        with _mock_comports([
            FakePort(device="/dev/ttyACM0", vid=_PICO_VID, pid=11, serial_number="X"),
            FakePort(device="/dev/ttyACM1", vid=_PICO_VID, pid=11, serial_number="X"),
        ]):
            assert auto_detect_port() == "/dev/ttyACM1"

    def test_single_cdc_port_still_works(self):
        """If firmware exposes only data (no console), the sole port wins."""
        with _mock_comports([
            FakePort(device="COM5", vid=_PICO_VID, pid=11, serial_number="ONLY"),
        ]):
            ports = list_pico_ports()
            assert len(ports) == 1
            assert ports[0]["is_data_port"] is True
            assert auto_detect_port() == "COM5"

    def test_two_picos_different_serials_each_gets_data_port(self):
        """Plug in 2 Picos → each is its own group → each has a data port.

        ``auto_detect_port`` returns the first one by enumeration order
        — users with multiple Picos must pass ``--port`` explicitly.
        """
        with _mock_comports([
            FakePort(device="/dev/cu.usbmodem11201", vid=_PICO_VID, pid=11,
                     serial_number="PICO_A"),
            FakePort(device="/dev/cu.usbmodem11203", vid=_PICO_VID, pid=11,
                     serial_number="PICO_A"),
            FakePort(device="/dev/cu.usbmodem21201", vid=_PICO_VID, pid=11,
                     serial_number="PICO_B"),
            FakePort(device="/dev/cu.usbmodem21203", vid=_PICO_VID, pid=11,
                     serial_number="PICO_B"),
        ]):
            ports = list_pico_ports()
            data_ports = [p["device"] for p in ports if p["is_data_port"]]
            assert sorted(data_ports) == [
                "/dev/cu.usbmodem11203",  # Pico A data
                "/dev/cu.usbmodem21203",  # Pico B data
            ]
            # First detected = first in enumeration. Caller should specify
            # --port explicitly when multiple Picos are present.
            assert auto_detect_port() in data_ports


class TestNonPicoIgnored:
    def test_bluetooth_console_not_pico(self):
        with _mock_comports([
            FakePort(device="/dev/cu.Bluetooth-Incoming-Port"),
            FakePort(device="/dev/cu.debug-console"),
        ]):
            ports = list_pico_ports()
            assert all(not p["likely_pico"] for p in ports)
            assert all(not p["is_data_port"] for p in ports)
            assert auto_detect_port() is None

    def test_mixed_pico_and_non_pico(self):
        with _mock_comports([
            FakePort(device="/dev/cu.Bluetooth-Incoming-Port"),
            FakePort(device="/dev/cu.usbmodem21201", vid=_PICO_VID, pid=11,
                     serial_number="ABC"),
            FakePort(device="/dev/cu.usbmodem21203", vid=_PICO_VID, pid=11,
                     serial_number="ABC"),
        ]):
            assert auto_detect_port() == "/dev/cu.usbmodem21203"


class TestCandidatesNeverIncludeTheConsole:
    """``auto_detect_ports`` is the server's try-list. It used to append every
    likely-Pico port after the data ports — which, with one board, is that
    board's REPL console. So when another program held the data port, the
    server connected to the console instead, reported "connected", and wrote
    protocol frames into the REPL, where the sequence bytes 0x03 / 0x04 are
    Ctrl-C / Ctrl-D: the firmware the other program was using got
    interrupted and restarted."""

    def test_one_board_offers_only_its_data_port(self):
        with _mock_comports([
            FakePort(device="COM5", vid=_PICO_VID, pid=11, serial_number="ABC"),
            FakePort(device="COM6", vid=_PICO_VID, pid=11, serial_number="ABC"),
        ]):
            assert auto_detect_ports() == ["COM6"]

    def test_two_boards_offer_one_data_port_each(self):
        with _mock_comports([
            FakePort(device="/dev/cu.usbmodem11201", vid=_PICO_VID, pid=11,
                     serial_number="PICO_A"),
            FakePort(device="/dev/cu.usbmodem11203", vid=_PICO_VID, pid=11,
                     serial_number="PICO_A"),
            FakePort(device="/dev/cu.usbmodem21201", vid=_PICO_VID, pid=11,
                     serial_number="PICO_B"),
            FakePort(device="/dev/cu.usbmodem21203", vid=_PICO_VID, pid=11,
                     serial_number="PICO_B"),
        ]):
            assert auto_detect_ports() == [
                "/dev/cu.usbmodem11203", "/dev/cu.usbmodem21203",
            ]

    def test_single_port_board_and_no_board(self):
        with _mock_comports([
            FakePort(device="COM5", vid=_PICO_VID, pid=11, serial_number="ONLY"),
        ]):
            assert auto_detect_ports() == ["COM5"]
        with _mock_comports([FakePort(device="/dev/cu.Bluetooth-Incoming-Port")]):
            assert auto_detect_ports() == []
            assert auto_detect_port() is None


class TestDataPortByInterfaceNumber:
    """Which port is the data channel is the USB interface number's to say —
    the console is declared first. Port numbers only usually agree: Windows
    hands out COM numbers from whatever is free, so a console can sit above
    its data port, and then "highest-numbered" picks the console."""

    def test_windows_console_numbered_above_data(self):
        # pyserial 3.5 on Windows, as measured: location "<usb path>:x.<iface>"
        with _mock_comports([
            FakePort(device="COM8", vid=_PICO_VID, pid=11, serial_number="ABC",
                     location="1-13:x.0"),
            FakePort(device="COM6", vid=_PICO_VID, pid=11, serial_number="ABC",
                     location="1-13:x.2"),
        ]):
            assert auto_detect_ports() == ["COM6"]
            assert auto_detect_port() == "COM6"

    def test_linux_location_format(self):
        with _mock_comports([
            FakePort(device="/dev/ttyACM1", vid=_PICO_VID, pid=11,
                     serial_number="X", location="1-1.4:1.0"),
            FakePort(device="/dev/ttyACM0", vid=_PICO_VID, pid=11,
                     serial_number="X", location="1-1.4:1.2"),
        ]):
            assert auto_detect_ports() == ["/dev/ttyACM0"]

    def test_interface_number_missing_on_one_port_falls_back_to_port_number(self):
        with _mock_comports([
            FakePort(device="COM8", vid=_PICO_VID, pid=11, serial_number="ABC",
                     location="1-13:x.0"),
            FakePort(device="COM6", vid=_PICO_VID, pid=11, serial_number="ABC"),
        ]):
            assert auto_detect_ports() == ["COM8"]


class TestBoardsWithoutSerialNumbers:
    """A board that reports no serial number is told apart by its place in
    the USB tree. Grouping every serial-less port together would leave one
    data port for all of them — and with the try-list no longer padded with
    every other Pico port, the other boards would drop out entirely."""

    def test_two_serial_less_dual_cdc_boards_each_offer_their_data_port(self):
        with _mock_comports([
            FakePort(device="COM10", vid=_PICO_VID, pid=11, location="1-2:x.0"),
            FakePort(device="COM11", vid=_PICO_VID, pid=11, location="1-2:x.2"),
            FakePort(device="COM20", vid=_PICO_VID, pid=11, location="1-3:x.0"),
            FakePort(device="COM21", vid=_PICO_VID, pid=11, location="1-3:x.2"),
        ]):
            assert auto_detect_ports() == ["COM11", "COM21"]

    def test_two_serial_less_single_port_boards_are_both_offered(self):
        with _mock_comports([
            FakePort(device="COM5", vid=_PICO_VID, pid=11, location="1-2:x.0"),
            FakePort(device="COM6", vid=_PICO_VID, pid=11, location="1-3:x.0"),
        ]):
            assert auto_detect_ports() == ["COM5", "COM6"]

    def test_macos_location_has_no_interface_but_still_separates_boards(self):
        # pyserial on macOS reports only the device's place in the USB tree
        # (no ":"), and the device name carries the interface.
        with _mock_comports([
            FakePort(device="/dev/cu.usbmodem11201", vid=_PICO_VID, pid=11,
                     location="17-2"),
            FakePort(device="/dev/cu.usbmodem11203", vid=_PICO_VID, pid=11,
                     location="17-2"),
            FakePort(device="/dev/cu.usbmodem21201", vid=_PICO_VID, pid=11,
                     location="33-2"),
            FakePort(device="/dev/cu.usbmodem21203", vid=_PICO_VID, pid=11,
                     location="33-2"),
        ]):
            assert auto_detect_ports() == [
                "/dev/cu.usbmodem11203", "/dev/cu.usbmodem21203",
            ]

    def test_a_port_whose_location_is_unreadable_keeps_the_old_grouping(self):
        # One port of a serial-less board has no location. Splitting by path
        # would leave the console alone in a group — marked as data, and
        # tried first. Instead every serial-less port shares one group, as
        # before, and the data port is still the only candidate.
        with _mock_comports([
            FakePort(device="COM5", vid=_PICO_VID, pid=11, location="1-13:x.0"),
            FakePort(device="COM6", vid=_PICO_VID, pid=11),
        ]):
            assert auto_detect_ports() == ["COM6"]

    def test_a_port_without_a_serial_joins_its_board_by_path(self):
        # The two ports of one board disagree on the serial number: the one
        # without it is matched to the board by the shared USB path.
        with _mock_comports([
            FakePort(device="COM5", vid=_PICO_VID, pid=11, serial_number="ABC",
                     location="1-13:x.0"),
            FakePort(device="COM6", vid=_PICO_VID, pid=11, location="1-13:x.2"),
        ]):
            assert auto_detect_ports() == ["COM6"]


@pytest.mark.asyncio
async def test_lazy_retry_does_not_open_the_console_either():
    """Every tool call on an unavailable board re-runs detection. That path
    must not reach for the console any more than startup does."""
    opened = []

    class FakeBridge:
        def __init__(self, port, baudrate=115200):
            self.port = port

        async def connect(self):
            opened.append(self.port)
            raise PermissionError("held by another program")

    server = ClawTouchMcpServer(ServerConfig(screen_w=1920, screen_h=1080))
    bridge = UnavailableBridge(server, tried_ports=[], baudrate=115200)
    with _mock_comports([
        FakePort(device="COM5", vid=_PICO_VID, pid=11, serial_number="ABC",
                 location="1-13:x.0"),
        FakePort(device="COM6", vid=_PICO_VID, pid=11, serial_number="ABC",
                 location="1-13:x.2"),
    ]), patch("clawtouch_mcp.server.SerialHidBridge", FakeBridge):
        assert await bridge._try_promote() is False
    assert opened == ["COM6"]
    assert bridge._tried_ports == ["COM6"]


@pytest.mark.asyncio
async def test_busy_data_port_is_not_swapped_for_the_console():
    """The whole bug end to end: the board's data port is held by another
    program, and the server must give up on that board — not open its
    console — so the next tool call reports it busy."""
    opened = []

    class FakeBridge:
        def __init__(self, port, baudrate=115200):
            self.port = port

        async def connect(self):
            opened.append(self.port)
            if self.port == "COM6":
                raise PermissionError("held by another program")

    # Explicit screen: no platform screen detection in a port test.
    server = ClawTouchMcpServer(ServerConfig(screen_w=1920, screen_h=1080))
    with _mock_comports([
        FakePort(device="COM5", vid=_PICO_VID, pid=11, serial_number="ABC"),
        FakePort(device="COM6", vid=_PICO_VID, pid=11, serial_number="ABC"),
    ]), patch("clawtouch_mcp.server.SerialHidBridge", FakeBridge):
        await server.start()
    assert opened == ["COM6"], "the console port must never be opened"
    assert isinstance(server.bridge, UnavailableBridge)
    assert server.bridge._tried_ports == ["COM6"]
