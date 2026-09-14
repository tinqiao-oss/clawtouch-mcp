# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Text the device cannot type is refused before any of it is sent.

The firmware presses one key per character on a US layout and stops at the
first character that has no key — after typing everything before it — and
the bridge used to carry on with the chunks after a failed one. A mixed
string therefore arrived as fragments around a gap, in a field that looked
finished. Now ``hid.type`` and the batch ``type`` op check the whole text
first, and the bridge stops at the first unacknowledged chunk.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from clawtouch_mcp.bridge import SerialHidBridge, untypable_chars
from clawtouch_mcp.protocol import CommandType
from clawtouch_mcp.server import (
    ClawTouchMcpServer,
    MockBridge,
    ServerConfig,
)


def _server():
    srv = ClawTouchMcpServer(ServerConfig(screen_w=1920, screen_h=1080, mock=True))
    srv.bridge = MockBridge()
    return srv


def _typed(srv):
    return [c for c in srv.bridge._calls if c[0] == "type"]


def test_untypable_characters_are_found_once_in_order():
    assert untypable_chars("Hello, world! ~`|\\{}[]<>?") == []
    # Control characters are stripped before sending, not refused.
    assert untypable_chars("a\nb\tc\r\x7f") == []
    assert untypable_chars("Hello，世界 世界") == [
        "，", "世", "界",
    ]
    # An emoji is one character (one code point), not two halves.
    assert untypable_chars("ok \U0001F44D\U0001F44D café") == [
        "\U0001F44D", "é",
    ]


def test_hid_type_refuses_non_ascii_and_sends_nothing():
    srv = _server()
    resp = asyncio.run(srv._on_tool_call({
        "name": "hid.type",
        "arguments": {"text": "Hello，世界"},
    }))
    assert resp["isError"] is True
    body = resp["content"][0]["text"]
    assert "nothing was typed" in body
    assert "'世'" in body, "the refusal must name what to rewrite"
    assert _typed(srv) == [], "not even the ASCII before it may go out"


def test_hid_type_still_types_ascii_and_controls():
    srv = _server()
    resp = asyncio.run(srv._on_tool_call({
        "name": "hid.type", "arguments": {"text": "hello\n"},
    }))
    assert resp["isError"] is False
    assert _typed(srv) == [("type", {"text": "hello\n"})]


def test_batch_type_op_refuses_non_ascii_without_typing():
    srv = _server()
    result = asyncio.run(srv._tool_batch(ops=[
        {"type": "type", "text": "hi 你好"},
    ]))
    assert result["ok"] is False
    op = result["results"][0]
    assert op["ok"] is False
    assert "nothing was typed" in op["error"]
    assert _typed(srv) == []


def _bridge_with_replies(replies):
    bridge = SerialHidBridge("COM_TEST")
    sent = []

    async def fake_send_raw(cmd, **_kw):
        sent.append(cmd)
        return replies[len(sent) - 1]

    bridge._send_raw = fake_send_raw
    return bridge, sent


def test_type_text_stops_at_the_first_unacknowledged_chunk():
    ack = SimpleNamespace(cmd_type=CommandType.ACK)
    err = SimpleNamespace(cmd_type=CommandType.ERROR)
    for failure in (err, None):          # a firmware ERROR, or no reply at all
        bridge, sent = _bridge_with_replies([ack, failure, ack])
        ok = asyncio.run(bridge.type_text("x" * 80, chunk_size=32))   # 3 chunks
        assert ok is False
        assert len(sent) == 2, "the chunk after a failed one must not be sent"
        # ...and how far it got is kept for whoever reports the failure
        assert bridge.last_type_confirmed == 32
        assert bridge.last_type_unconfirmed == 32


def test_a_failed_type_reports_what_the_device_confirmed():
    """The requested count is not what happened once typing stops early:
    report the acknowledged characters, and the chunk that may be partly
    typed, so a retry does not type the confirmed part again."""
    srv = _server()
    srv.bridge = SimpleNamespace(
        type_text=AsyncMock(return_value=False),
        last_type_confirmed=32, last_type_unconfirmed=32,
        last_error_detail="ACK timeout after 1.0s",
    )
    resp = asyncio.run(srv._on_tool_call({
        "name": "hid.type", "arguments": {"text": "x" * 80},
    }))
    assert resp["isError"] is True
    body = json.loads(resp["content"][0]["text"])
    assert body["chars"] == 32 and body["unconfirmed_chars"] == 32
    batch = asyncio.run(srv._tool_batch(ops=[{"type": "type", "text": "x" * 80}]))
    op = batch["results"][0]
    assert op["chars"] == 32 and op["unconfirmed_chars"] == 32


def test_the_bridge_itself_refuses_untypeable_text():
    """Callers that skip the server — the computer-use demos call
    ``bridge.type_text`` directly — get the same refusal, before any frame."""
    ack = SimpleNamespace(cmd_type=CommandType.ACK)
    bridge, sent = _bridge_with_replies([ack, ack])
    try:
        asyncio.run(bridge.type_text("hi 你好"))
    except ValueError as err:
        assert "nothing was typed" in str(err)
    else:
        raise AssertionError("untypeable text was accepted")
    assert sent == []


def test_type_text_sends_every_chunk_when_all_are_acknowledged():
    ack = SimpleNamespace(cmd_type=CommandType.ACK)
    bridge, sent = _bridge_with_replies([ack, ack, ack])
    assert asyncio.run(bridge.type_text("x" * 80, chunk_size=32)) is True
    assert len(sent) == 3
