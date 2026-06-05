# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Tests for the no-Pillow screenshot fallback (v0.4.3).

Background — real-world report (macOS, Tencent WorkBuddy's bundled
py3.13/arm64): the host Python ships a hardened runtime with *library
validation*, which only allows loading native extensions signed with the
host's own Team ID. Pillow's ``_imaging.cpython-313-darwin.so`` is signed by
someone else, so ``from PIL import Image`` fails with::

    ImportError: dlopen(.../PIL/_imaging...so): code signature ... not valid
    for use in process: ... have different Team IDs

mss is pure-Python (ctypes → CoreGraphics, a *platform* framework that's
exempt), so it loads fine. These tests pin the behaviour: ``auto`` degrades
Pillow→mss-png, produces a valid PNG decimated to logical resolution, reports
``backend`` + honest scale, and surfaces a human-readable note instead of the
dlopen wall of text — without ever raising on the degrade path.
"""
from __future__ import annotations

import asyncio
import struct
import sys
import types
import zlib

import pytest

from clawtouch_mcp.server import (
    ClawTouchMcpServer,
    ImageResult,
    MockBridge,
    ServerConfig,
    _decimate_rgb,
)

# Small "Retina" dims keep zlib-on-random fast; the 2x collapse is the point.
LOGICAL_W, LOGICAL_H = 40, 20
PHYSICAL_W, PHYSICAL_H = 80, 40


def _encode_png(rgb: bytes, size) -> bytes:
    """Minimal real PNG encoder (RGB, 8-bit) — stands in for the pure-Python
    ``mss.tools.to_png`` so the no-Pillow path yields a *valid* PNG we can
    assert on without needing mss installed in the test env."""
    w, h = size
    stride = w * 3
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter type 0 (None) per scanline
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # color type 2 = RGB
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))


def _png_dims(png: bytes):
    """Parse width/height out of the IHDR chunk of a PNG."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    # IHDR data starts at byte 16 (8 sig + 4 len + 4 'IHDR').
    w, h = struct.unpack(">II", png[16:24])
    return w, h


def _fake_mss_module():
    """A ``mss``-shaped fake: monitors[1] reports LOGICAL, grab() returns a
    PHYSICAL-sized FakeShot, and ``tools.to_png`` is a *real* encoder."""
    class FakeShot:
        def __init__(self, w: int, h: int):
            self.width = w
            self.height = h
            self.size = (w, h)
            # Deterministic non-uniform content so decimation actually has
            # distinct pixels to sample (and PNG size is meaningful).
            self.rgb = bytes((i * 7 + 13) & 0xFF for i in range(w * h * 3))

    class FakeMSS:
        def __init__(self):
            self.monitors = [
                {"left": 0, "top": 0, "width": 0, "height": 0},
                {"left": 0, "top": 0, "width": LOGICAL_W, "height": LOGICAL_H},
            ]

        def grab(self, monitor):
            return FakeShot(PHYSICAL_W, PHYSICAL_H)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    fake = types.ModuleType("mss")
    fake.MSS = FakeMSS
    fake.tools = types.SimpleNamespace(to_png=_encode_png)
    return fake


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", _fake_mss_module())
    cfg = ServerConfig(screen_w=LOGICAL_W, screen_h=LOGICAL_H,
                       mock=True, allow_screenshot=True)
    s = ClawTouchMcpServer(cfg)
    s.bridge = MockBridge()
    return s


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_LIBVAL_ERR = ImportError(
    "dlopen(.../PIL/_imaging.cpython-313-darwin.so): code signature in "
    "(...) not valid for use in process: ... (non-platform) have different "
    "Team IDs"
)


# ── auto degrade ----------------------------------------------------

class TestAutoDegrade:
    def test_pillow_dlopen_reject_degrades_to_mss_png(self, srv, monkeypatch):
        def _boom():
            raise _LIBVAL_ERR
        monkeypatch.setattr(srv, "_probe_pillow", _boom)

        result = _run(srv._tool_screenshot())
        assert isinstance(result, ImageResult)
        assert result.mime_type == "image/png"
        assert result.metadata["backend"] == "mss-png"
        # Valid PNG, decimated to logical resolution.
        assert result.image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        assert _png_dims(result.image_bytes) == (LOGICAL_W, LOGICAL_H)
        assert result.metadata["width"] == LOGICAL_W
        assert result.metadata["raw_size"] == [PHYSICAL_W, PHYSICAL_H]
        # Scale honest + collapsed (decimated to logical → 1.0).
        assert result.metadata["scale_x"] == 1.0
        assert result.metadata["scale_y"] == 1.0
        # Friendly diagnostic, not the dlopen wall of text.
        note = result.metadata["note"]
        assert "mss-png" in note
        assert "library validation" in note
        assert "entitlement" in note

    def test_degrade_does_not_error_through_dispatch(self, srv, monkeypatch):
        monkeypatch.setattr(srv, "_probe_pillow",
                            lambda: (_ for _ in ()).throw(_LIBVAL_ERR))
        resp = _run(srv._on_tool_call(
            {"name": "hid.screenshot", "arguments": {}}))
        assert resp["isError"] is False
        kinds = [c["type"] for c in resp["content"]]
        assert "image" in kinds and "text" in kinds

    def test_backend_probe_cached_not_retried(self, srv, monkeypatch):
        calls = {"n": 0}

        def _boom():
            calls["n"] += 1
            raise _LIBVAL_ERR
        monkeypatch.setattr(srv, "_probe_pillow", _boom)
        _run(srv._tool_screenshot())
        _run(srv._tool_screenshot())
        assert calls["n"] == 1  # probed once, then cached

    def test_missing_pillow_message_differs_from_libval(self, srv, monkeypatch):
        monkeypatch.setattr(srv, "_probe_pillow",
                            lambda: (_ for _ in ()).throw(ImportError("No module named 'PIL'")))
        result = _run(srv._tool_screenshot())
        assert result.metadata["backend"] == "mss-png"
        note = result.metadata["note"]
        assert "not installed" in note
        assert "library validation" not in note


# ── forced backends -------------------------------------------------

class TestForcedBackend:
    def test_force_mss_png(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "mss", _fake_mss_module())
        cfg = ServerConfig(screen_w=LOGICAL_W, screen_h=LOGICAL_H,
                           mock=True, allow_screenshot=True,
                           screenshot_backend="mss-png")
        s = ClawTouchMcpServer(cfg)
        s.bridge = MockBridge()
        # format='png' so there's no jpeg→png note either — a clean forced
        # path with no degrade and no format coercion.
        result = _run(s._tool_screenshot(format="png"))
        assert result.metadata["backend"] == "mss-png"
        assert result.image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        # No degrade note when explicitly forced (didn't fall back).
        assert "note" not in result.metadata

    def test_jpeg_request_on_mss_png_returns_png(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "mss", _fake_mss_module())
        cfg = ServerConfig(screen_w=LOGICAL_W, screen_h=LOGICAL_H,
                           mock=True, allow_screenshot=True,
                           screenshot_backend="mss-png")
        s = ClawTouchMcpServer(cfg)
        s.bridge = MockBridge()
        result = _run(s._tool_screenshot(format="jpeg"))
        assert result.mime_type == "image/png"
        assert result.metadata["format"] == "png"
        assert "JPEG needs Pillow" in result.metadata["note"]

    def test_force_pillow_unavailable_raises_friendly(self, srv, monkeypatch):
        srv.config.screenshot_backend = "pillow"
        monkeypatch.setattr(srv, "_probe_pillow",
                            lambda: (_ for _ in ()).throw(_LIBVAL_ERR))
        with pytest.raises(RuntimeError, match="library validation"):
            _run(srv._tool_screenshot())

    def test_invalid_backend_value_rejected(self, srv):
        srv.config.screenshot_backend = "imagemagick"
        with pytest.raises(ValueError, match="auto|pillow|mss-png"):
            _run(srv._tool_screenshot())


# ── decimation unit -------------------------------------------------

class TestDecimate:
    def test_factor_two_halves_dimensions(self):
        rgb = bytes((i & 0xFF) for i in range(8 * 4 * 3))  # 8x4 RGB
        out_w, out_h, out = _decimate_rgb(rgb, 8, 4, 4, 2)
        assert (out_w, out_h) == (4, 2)
        assert len(out) == 4 * 2 * 3

    def test_factor_one_returns_input_unchanged(self):
        rgb = bytes(range(6 * 6 * 3 % 256)) * 99
        rgb = rgb[:6 * 6 * 3]
        out_w, out_h, out = _decimate_rgb(rgb, 6, 6, 6, 6)
        assert (out_w, out_h) == (6, 6)
        assert out is rgb  # no copy when f<=1

    def test_keeps_topleft_pixel(self):
        # Pixel (0,0) must survive decimation (stride 0 sample).
        rgb = bytearray(4 * 4 * 3)
        rgb[0:3] = b"\x11\x22\x33"
        _, _, out = _decimate_rgb(bytes(rgb), 4, 4, 2, 2)
        assert out[0:3] == b"\x11\x22\x33"

    def test_cap_only_band_ceil_forces_decimation(self):
        # raw 3024x1964 (5.94M); cap target 2481x1611 is a 1.22x shrink that
        # round() collapsed to f=1 (skip → cap bypassed). ceil() forces f=2 so
        # the output lands under the target / cap.
        rgb = bytes(3024 * 1964 * 3)
        out_w, out_h, _ = _decimate_rgb(rgb, 3024, 1964, 2481, 1611)
        assert (out_w, out_h) == (1512, 982)
        assert out_w <= 2481 and out_h <= 1611


def _make_fake_mss(pw, ph, lw, lh):
    """Fake mss with custom physical/logical dims + the real PNG encoder."""
    class FakeShot:
        def __init__(self):
            self.width, self.height, self.size = pw, ph, (pw, ph)
            self.rgb = bytes(pw * ph * 3)  # zeros — fast to_png; dims are the point

    class FakeMSS:
        def __init__(self):
            self.monitors = [
                {"left": 0, "top": 0, "width": 0, "height": 0},
                {"left": 0, "top": 0, "width": lw, "height": lh},
            ]

        def grab(self, monitor):
            return FakeShot()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    fake = types.ModuleType("mss")
    fake.MSS = FakeMSS
    fake.tools = types.SimpleNamespace(to_png=_encode_png)
    return fake


class TestMssPngCapEnforced:
    """0.4.3 regression fix: integer decimation with round() let a cap-only
    shrink (raw in the 4M–9M px band) collapse to f=1, bypassing the 4M output
    cap and returning a full-res multi-MB PNG. ceil() keeps mss-png under cap."""

    def test_over_cap_frame_stays_under_cap(self, monkeypatch):
        # 2100x2000 = 4.2M physical; screen == physical so NO logical downsample
        # applies (raw < screen*1.2) — only the 4M cap can shrink it.
        PW, PH = 2100, 2000
        monkeypatch.setitem(sys.modules, "mss", _make_fake_mss(PW, PH, PW, PH))
        cfg = ServerConfig(screen_w=PW, screen_h=PH, mock=True,
                           allow_screenshot=True, screenshot_backend="mss-png")
        s = ClawTouchMcpServer(cfg)
        s.bridge = MockBridge()
        result = _run(s._tool_screenshot(format="png"))
        w, h = result.metadata["width"], result.metadata["height"]
        assert w * h <= 4_000_000, f"cap bypassed: {w}x{h} = {w * h}px"
        assert (w, h) != (PW, PH), "returned full-res — cap was a no-op"
        assert result.metadata["raw_size"] == [PW, PH]
        assert result.image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
