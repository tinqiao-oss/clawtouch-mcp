"""Shared pytest fixtures.

The autouse ``_fake_cursor_for_tests`` fixture sets
``CLAWTOUCH_FAKE_CURSOR`` in the test process environment so every
test (and every subprocess spawned by stdio integration tests) sees
a deterministic cursor position when ``hid.click`` / ``hid.move`` /
``hid.hover`` exercise their absolute-coordinate path. Real OS
cursor queries (Win32 / CoreGraphics / X11) are not invoked under
test, which keeps headless CI from flaking.

Tests that specifically want to test the missing-cursor / error path
should ``monkeypatch.delenv("CLAWTOUCH_FAKE_CURSOR", raising=False)``
inside their body.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _fake_cursor_for_tests(monkeypatch):
    # Centred on a 1920x1080 screen so deltas in tests come out as
    # `target - 960` / `target - 540` — easy to reason about.
    monkeypatch.setenv("CLAWTOUCH_FAKE_CURSOR", "960,540")
    yield
    # monkeypatch auto-restores on teardown
