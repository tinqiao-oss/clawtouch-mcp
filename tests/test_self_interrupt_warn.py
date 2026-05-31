# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Self-interrupt heads-up.

The server warns ONCE (warn-only, never blocks) when it sends a quit/close
combo that would kill the agent if the MCP server shares a machine with it
and the agent app is frontmost. Real USB HID has no app addressing, so
``hid.key("cmd+q")`` hits whatever window is focused.

Covers the pure detection matrix plus the one-shot warn behavior through
the hid.key / hid.key_press handlers, asserting the keystroke is ALWAYS
still delivered to the bridge (warn-only, not a block).
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from clawtouch_mcp.server import (
    ClawTouchMcpServer,
    MockBridge,
    ServerConfig,
    _is_self_interrupt_combo,
)

LOGGER_NAME = "clawtouch_mcp.server"


@pytest.fixture
def server():
    srv = ClawTouchMcpServer(ServerConfig(screen_w=1920, screen_h=1080, mock=True))
    srv.bridge = MockBridge()
    return srv


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _warns(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "quit/close combo" in r.getMessage()]


# ── pure detection matrix ──

@pytest.mark.parametrize("mods,key,expected", [
    (["cmd"], "q", True),           # macOS quit
    (["gui"], "q", True),           # gui / win / cmd all map to the GUI key
    (["win"], "q", True),
    (["cmd"], "Q", True),           # case-insensitive
    (["cmd", "shift"], "q", True),  # extra modifiers still match
    (["alt"], "f4", True),          # Windows quit
    (["alt"], "F4", True),
    (["cmd"], "c", False),          # cmd+c (copy) is fine
    (["ctrl"], "q", False),         # ctrl+q is not the quit combo
    ([], "q", False),               # bare q
    (["alt"], "f5", False),         # alt+f5 is not quit
    (["cmd"], "w", False),          # close-tab is risky but legit -> not warned
])
def test_detection_matrix(mods, key, expected):
    assert _is_self_interrupt_combo(mods, key) is expected


# ── one-shot, warn-only behavior through the handlers ──

def test_cmd_q_warns_once_and_still_sends(server, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        _run(server._tool_key(key="cmd+q"))
    assert len(_warns(caplog)) == 1
    # warn-only: the keystroke was STILL delivered to the bridge
    combos = [c for c in server.bridge._calls if c[0] == "key"]
    assert combos
    assert combos[-1][1]["key"] == "q"
    assert "cmd" in combos[-1][1]["modifiers"]


def test_alt_f4_via_modifiers_array_warns(server, caplog):
    # combo supplied through the modifiers array, not the "+" shorthand
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        _run(server._tool_key(key="f4", modifiers=["alt"]))
    assert len(_warns(caplog)) == 1


def test_warning_is_one_shot(server, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        _run(server._tool_key(key="cmd+q"))
        _run(server._tool_key(key="alt+f4"))
        _run(server._tool_key(key="cmd+q"))
    # three quit combos sent, only the first warns
    assert len(_warns(caplog)) == 1
    # ...and all three were delivered (never blocked)
    assert len([c for c in server.bridge._calls if c[0] == "key"]) == 3


def test_normal_and_escape_never_warn(server, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        _run(server._tool_key(key="cmd+c"))     # copy
        _run(server._tool_key(key="escape"))    # interrupt is recoverable -> not warned
        _run(server._tool_key(key="cmd+w"))     # close-tab -> not warned (too legit/noisy)
    assert _warns(caplog) == []


def test_key_press_path_also_warns(server, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        _run(server._tool_key_press(key="q", modifiers=["cmd"]))
    assert len(_warns(caplog)) == 1
    assert [c for c in server.bridge._calls if c[0] == "key_press"]
