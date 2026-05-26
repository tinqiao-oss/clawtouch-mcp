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

from clawtouch_mcp.bridge import (
    _PICO_VID,
    _port_sort_key,
    auto_detect_port,
    list_pico_ports,
)


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
