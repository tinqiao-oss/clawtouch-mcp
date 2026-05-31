# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""Shared pytest fixtures.

The autouse ``_fake_cursor_for_tests`` fixture sets
``CLAWTOUCH_FAKE_CURSOR`` in the test process environment so every
test (and every subprocess spawned by stdio integration tests) sees
a deterministic cursor position when ``hid.click`` / ``hid.move`` /
``hid.hover`` exercise their absolute-coordinate path. Real OS
cursor queries (Win32 / CoreGraphics / X11) are not invoked under
test, which keeps headless CI from flaking.

The closed-loop convergence path (``_converge_to_target``) needs the
mock cursor to reflect each emitted delta — ``MockBridge.mouse_move``
seeds + updates ``cursor._FAKE_DYNAMIC_STATE`` on the first call so
the converge loop terminates in one iteration under perfect mock
hardware. This fixture clears that dynamic state between tests so
state from one test never leaks into the next.

Tests that specifically want to test the missing-cursor / error path
should ``monkeypatch.delenv("CLAWTOUCH_FAKE_CURSOR", raising=False)``
inside their body AND call ``cursor._clear_fake_cursor()`` if a
prior ``MockBridge.mouse_move`` already seeded it.
"""
import importlib.util as _ilu

import pytest

from clawtouch_mcp import cursor as _cursor_mod

# Async tests (test_release_on_idle / test_unavailable_bridge) require
# pytest-asyncio. Without it pytest silently SKIPS them, producing a
# false-green "N passed, M skipped". Fail loudly at collection instead.
# Install the test extra: pip install -e ".[test]"
if _ilu.find_spec("pytest_asyncio") is None:  # pragma: no cover
    raise RuntimeError(
        "pytest-asyncio is not installed — the async tests would silently "
        'skip. Install the test extra: pip install -e ".[test]"'
    )


@pytest.fixture(autouse=True)
def _fake_cursor_for_tests(monkeypatch):
    # Centred on a 1920x1080 screen so deltas in tests come out as
    # `target - 960` / `target - 540` — easy to reason about.
    monkeypatch.setenv("CLAWTOUCH_FAKE_CURSOR", "960,540")
    # Always start each test with a clean dynamic state — prior tests
    # may have left MockBridge-seeded state behind.
    _cursor_mod._clear_fake_cursor()
    yield
    _cursor_mod._clear_fake_cursor()
