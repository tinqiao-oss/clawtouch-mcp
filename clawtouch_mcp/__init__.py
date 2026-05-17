"""ClawTouch MCP — standalone MCP server exposing HID mouse/keyboard tools.

Public API:
    clawtouch_mcp.protocol   — HID wire protocol v1.0
    clawtouch_mcp.bridge     — Async serial bridge to Pico 2
    clawtouch_mcp.server     — MCP stdio JSON-RPC server + tool registry
"""

__version__ = "0.2.3"
__all__ = ["__version__"]
