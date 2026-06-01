# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Regression test for the Retina-screen MCP buffer overflow fix.

Bug history (pre-fix, see CHANGELOG v0.2.5):
    On macOS Retina, ``mss.MSS().monitors[1]`` reports LOGICAL points
    (e.g. 1512x982) while ``sct.grab(monitor)`` returns the PHYSICAL
    pixel buffer (e.g. 3024x1964 on a 2x scale). The pre-cap check
    used the LOGICAL pixel count, so it passed (1.48M < 4M cap), and
    a multi-MB base64 PNG ended up in the tool result — overflowing
    MCP client text buffers (Claude Desktop / Claude Code truncated
    the result and the agent never saw the image).

Fix (in this file's scope):
    * ``_tool_screenshot`` returns an ``ImageResult`` marker.
    * ``_on_tool_call`` translates that into MCP-standard
      ``{"type": "image", "data": ..., "mimeType": ...}`` content,
      so the client routes it through the vision-token path instead
      of stuffing base64 into the tool-result text envelope.
    * On full-screen captures the physical buffer is auto-resized
      to the configured logical screen size (Retina collapses 2x).
    * After-resize cap (4M output pixels) protects against giant
      region requests on real 4K monitors.
    * Default format is JPEG q80; ``format='png'`` opts back into
      lossless for OCR-grade work.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

from clawtouch_mcp.server import (
    ClawTouchMcpServer,
    ImageResult,
    MockBridge,
    ServerConfig,
)


# ── Retina simulator -------------------------------------------------

LOGICAL_W, LOGICAL_H = 1512, 982       # what mss.monitors[1] reports
PHYSICAL_W, PHYSICAL_H = 3024, 1964    # what sct.grab() actually returns


def _fake_mss_module():
    """Build a ``mss``-shaped fake module that simulates Retina.

    The handler under test imports mss inside the function and calls
    ``mss.MSS()`` then ``sct.grab(monitor)``. Our fake reports LOGICAL
    dims in ``monitors[1]`` and returns PHYSICAL-sized FakeShot from
    grab — exactly the units mismatch that caused the original bug."""
    class FakeShot:
        def __init__(self, w: int, h: int):
            self.width = w
            self.height = h
            self.size = (w, h)
            # Random noise — worst case for PNG compression, matches
            # how a real busy desktop looks. Asserting on size here
            # is meaningful precisely because empty/solid pixels
            # would compress to near-nothing and hide the regression.
            self.rgb = os.urandom(w * h * 3)

    class FakeMSS:
        def __init__(self):
            self.monitors = [
                {"left": 0, "top": 0, "width": 0, "height": 0},
                {"left": 0, "top": 0,
                 "width": LOGICAL_W, "height": LOGICAL_H},
            ]

        def grab(self, monitor):
            # mss on Retina ignores the logical monitor dict and
            # returns physical pixels.
            return FakeShot(PHYSICAL_W, PHYSICAL_H)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    fake = types.ModuleType("mss")
    fake.MSS = FakeMSS
    # ``mss.tools.to_png`` is no longer used by the fixed code (Pillow
    # encodes directly), so we don't have to mock it. Leave a stub so
    # any accidental import doesn't crash.
    fake.tools = types.SimpleNamespace(to_png=lambda *a, **kw: b"")
    return fake


# ── Test fixture & helpers ------------------------------------------

@pytest.fixture
def retina_server(monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", _fake_mss_module())
    cfg = ServerConfig(
        screen_w=LOGICAL_W, screen_h=LOGICAL_H,
        mock=True, allow_screenshot=True,
    )
    srv = ClawTouchMcpServer(cfg)
    srv.bridge = MockBridge()
    return srv


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Assertions on the FIXED behavior --------------------------------

class TestRetinaScreenshotAutoResize:
    """After P1 fix: Retina captures collapse to logical resolution
    automatically; scale_x / scale_y are ~1.0; tool result rides on
    MCP image content type (not the text envelope)."""

    def test_returns_image_result_marker(self, retina_server):
        """Handler returns ``ImageResult`` so dispatch can route it
        through MCP image content."""
        result = _run(retina_server._tool_screenshot())
        assert isinstance(result, ImageResult)

    def test_output_resized_to_logical(self, retina_server):
        """Physical 3024x1964 grab is auto-resized to logical
        1512x982 — that's the Retina half-size collapse."""
        result = _run(retina_server._tool_screenshot())
        assert result.metadata["width"] == LOGICAL_W
        assert result.metadata["height"] == LOGICAL_H
        # ``raw_size`` is exposed so an agent can tell when a
        # resize happened (it will if width/height != raw_size).
        assert result.metadata["raw_size"] == [PHYSICAL_W, PHYSICAL_H]

    def test_scale_collapses_to_one_after_resize(self, retina_server):
        """Screenshot pixels == click coords after the resize, so
        agents that divide by scale_x/y get the same value back."""
        result = _run(retina_server._tool_screenshot())
        assert result.metadata["scale_x"] == 1.0
        assert result.metadata["scale_y"] == 1.0

    def test_default_format_is_jpeg(self, retina_server):
        result = _run(retina_server._tool_screenshot())
        assert result.mime_type == "image/jpeg"
        assert result.metadata["format"] == "jpeg"

    def test_payload_size_is_sane(self, retina_server):
        """JPEG q80 of a 1512x982 random-noise image lands well under
        1 MB — the original buggy code returned >1 MB of base64 PNG.
        We assert raw bytes (not base64) here; either way the
        post-base64 payload is comfortably inside the MCP text limit
        any client uses in practice."""
        result = _run(retina_server._tool_screenshot())
        # JPEG of random noise is the *worst* case (no spatial
        # correlation to exploit) and still encodes well under 1 MB.
        assert len(result.image_bytes) < 1_000_000, (
            f"payload bloat: {len(result.image_bytes)} bytes — "
            "is the resize step still working?"
        )

    def test_png_opt_in_still_works(self, retina_server):
        result = _run(retina_server._tool_screenshot(format="png"))
        assert result.mime_type == "image/png"
        assert result.metadata["format"] == "png"

    def test_bad_format_rejected(self, retina_server):
        with pytest.raises(ValueError, match="format must be"):
            _run(retina_server._tool_screenshot(format="webp"))


class TestRetinaScreenshotDispatchEnvelope:
    """The handler returns ImageResult; the dispatch layer's job is
    to translate that into MCP-standard image content. These tests
    pin the wire-format that Claude Desktop / Claude Code see."""

    def test_dispatch_wraps_as_image_content_type(self, retina_server):
        """``tools/call`` response carries ``type: image`` + the
        metadata as a sibling ``type: text`` entry."""
        response = _run(retina_server._on_tool_call({
            "name": "hid.screenshot",
            "arguments": {},
        }))
        assert response["isError"] is False
        content = response["content"]
        assert len(content) == 2
        image_entry, text_entry = content
        assert image_entry["type"] == "image"
        assert image_entry["mimeType"] == "image/jpeg"
        # Base64-encoded payload is present and non-trivial.
        assert len(image_entry["data"]) > 100
        # Metadata sibling.
        assert text_entry["type"] == "text"
        import json
        meta = json.loads(text_entry["text"])
        assert meta["width"] == LOGICAL_W
        assert meta["height"] == LOGICAL_H
        assert meta["format"] == "jpeg"


class TestRegionStillWorks:
    """Region captures bypass the logical-resize step (their pixel
    space is already the agent's intended frame) but still hit the
    4M cap as a defence."""

    def test_region_returns_image_result(self, retina_server):
        result = _run(retina_server._tool_screenshot(
            region=[100, 100, 500, 500]
        ))
        assert isinstance(result, ImageResult)
        # 400 x 400 region — should not be resized (well under cap,
        # and resize-to-logical only applies for full-screen).
        # The fake mss.grab returns PHYSICAL_W x PHYSICAL_H regardless
        # of monitor dict, so for this test we mainly assert no crash
        # and meaningful metadata. The full behaviour matrix for
        # region clamping is covered in test_server.py.
        assert result.metadata["raw_size"] == [PHYSICAL_W, PHYSICAL_H]
