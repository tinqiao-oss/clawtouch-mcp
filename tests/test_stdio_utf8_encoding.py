# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Regression test: stdio frames must be UTF-8 on every host locale.

Bug history (pre-fix):
    ``_write_message``'s line-delimited (newline) branch wrote the JSON
    via ``writer.write(data + "\\n")`` — i.e. through the TextIOWrapper,
    which encodes with the process's *locale* code page. On a non-UTF-8
    console (cp936 / GBK on Chinese Windows, where ``sys.stdout.encoding``
    defaults to ``'gbk'`` for a piped stdout) any non-ASCII byte in the
    JSON got GBK-encoded. A single em-dash in a tool description was
    enough: ``tools/list`` came back as GBK and a UTF-8 MCP client raised
    ``UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa1`` and the
    session never established. The framed (Content-Length) branch was
    already correct because it wrote ``data.encode("utf-8")`` via
    ``writer.buffer``; only the newline branch — the MCP-stdio default —
    was affected.

Fix:
    Both branches now write UTF-8 *bytes* via ``writer.buffer``, so the
    wire encoding is UTF-8 regardless of the host's locale/code page.
"""
import io
import json

import pytest

from clawtouch_mcp.server import _write_message


class _FakeStdout:
    """Stand-in for ``sys.stdout`` whose ``.buffer`` captures raw bytes.

    ``.write`` raises so the test *proves* the writer never falls back to
    the locale-encoded text path — that fallback was the bug.
    """

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, s):  # noqa: D401 - intentional trap
        raise AssertionError(
            "stdio must write UTF-8 bytes via .buffer, never text via "
            ".write() (that re-encodes with the host locale / code page)"
        )

    def flush(self) -> None:
        pass


_MSG = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {"desc": "physical HID input — pick the data port (ASCII colon: ok)"},
}


def test_line_mode_emits_strict_utf8():
    w = _FakeStdout()
    _write_message(w, _MSG, framed=False)
    raw = w.buffer.getvalue()
    # Must decode as STRICT UTF-8 (no errors=) and round-trip the em-dash.
    decoded = raw.decode("utf-8")
    assert "—" in decoded
    assert raw.endswith(b"\n")
    assert json.loads(decoded)["result"]["desc"].endswith("ok)")


def test_framed_mode_emits_strict_utf8():
    w = _FakeStdout()
    _write_message(w, _MSG, framed=True)
    raw = w.buffer.getvalue()
    header, body = raw.split(b"\r\n\r\n", 1)
    assert header.startswith(b"Content-Length:")
    # Declared length must match the UTF-8 byte length of the body.
    declared = int(header.split(b":", 1)[1].strip())
    assert declared == len(body)
    assert body.decode("utf-8").count("—") == 1


@pytest.mark.parametrize("framed", [False, True])
def test_nonascii_survives_byte_for_byte(framed):
    """A Chinese-heavy payload must come back identical after a strict
    UTF-8 decode — the exact failure mode an MCP client hits."""
    w = _FakeStdout()
    msg = {"jsonrpc": "2.0", "id": 2, "result": {"text": "你好，ClawTouch 上线了！— emoji 🐾"}}
    _write_message(w, msg, framed=framed)
    raw = w.buffer.getvalue()
    if framed:
        raw = raw.split(b"\r\n\r\n", 1)[1]
    assert json.loads(raw.decode("utf-8"))["result"]["text"] == msg["result"]["text"]
