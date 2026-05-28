# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""RateLimiter sliding-window behavior."""
from __future__ import annotations

import time

import pytest

from clawtouch_mcp.server import RateLimiter


def test_under_limit_passes():
    rl = RateLimiter(ops_per_sec=5)
    for _ in range(5):
        rl.check()  # no raise


def test_at_limit_raises_on_next_call():
    rl = RateLimiter(ops_per_sec=3)
    rl.check()
    rl.check()
    rl.check()
    with pytest.raises(RuntimeError, match="rate limit"):
        rl.check()


def test_window_slides_after_1s():
    rl = RateLimiter(ops_per_sec=2)
    rl.check()
    rl.check()
    time.sleep(1.05)  # window expires
    rl.check()  # no raise
