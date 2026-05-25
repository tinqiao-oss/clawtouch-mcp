# Security Policy

`clawtouch-mcp` runs as a local stdio process and exposes mouse /
keyboard primitives to whatever MCP client launches it. A bug here
could let a misbehaving agent or a malicious tool call:

- bypass the `--screen WxH` coordinate clamp (move the mouse off-
  screen, hit a coordinate the user never intended);
- bypass the `--ops-per-sec` rate limit (flood the host with input);
- send arbitrary HID reports via the open USB-CDC channel;
- trigger arbitrary file writes if the screenshot tool's `region`
  parameter were under-validated.

We take these seriously. **Please report vulnerabilities privately
first** so they can be patched before public disclosure.

## Supported versions

Only the **latest 0.x release** receives security fixes. We do not
back-port to older versions during the 0.x line. Once 1.0 ships, this
policy will be revised.

## How to report

Email **`support@tinqiao.com`** with subject prefix `[SECURITY]
clawtouch-mcp`.

Please include:

- The version (`pip show clawtouch-mcp`).
- A minimal reproduction (a JSON-RPC frame or short script — please
  do not include screenshots of confidential data).
- The impact you observed.
- Whether you intend to publish a CVE / blog post and your preferred
  disclosure timeline.

We'll acknowledge within **3 business days** and aim to ship a fix in
the next patch release (typically within 2 weeks for confirmed issues).

## What is NOT in scope

- **The behavior of LLM agents that use this server.** If an agent
  uses `hid.click` to do something the user didn't want, that's an
  agent / prompt issue, not an MCP-server issue.
- **Physical security of the Pico hardware.** Anyone with physical
  access to the device can do anything its USB HID profile allows
  regardless of this server.

## Disclosure

After a fix is released, we'll credit the reporter in the patch
release notes unless they request anonymity. Reports that turn out to
be intended behavior get a polite reply and a thank-you.
