# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Tinqiao Technology (Beijing) Co., Ltd.
"""ClawTouch MCP — standalone MCP server exposing HID mouse/keyboard tools.

Public API:
    clawtouch_mcp.protocol   — HID wire protocol v1.1 (additive over v1.0 baseline)
    clawtouch_mcp.bridge     — Async serial bridge to Pico 2
    clawtouch_mcp.server     — MCP stdio JSON-RPC server + tool registry
"""

__version__ = "0.3.0"
__all__ = ["__version__"]
