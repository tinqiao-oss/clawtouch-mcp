# Container image for registry/CI introspection (e.g. Glama) — NOT a runtime target.
#
# A ClawTouch HID device is real USB hardware, which a container cannot reach.
# So this image starts the MCP server in --mock mode: it exposes the full set of
# MCP tools (move, click, drag, type, key combos, scroll) and answers
# introspection requests without any physical device attached.
#
# For real use, install on the host instead:  pip install clawtouch-mcp
FROM python:3.12-slim
RUN pip install --no-cache-dir clawtouch-mcp
ENTRYPOINT ["clawtouch-mcp", "--mock"]
