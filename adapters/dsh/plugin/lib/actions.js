/**
 * The tools that send input straight to the server — typing, keys,
 * scrolling — as plain functions, so they are tested without a host
 * (index.js imports the host's `@deepseek-ai/dsh-tools`; this does not).
 *
 * Each one reports only input that happened. A dry run and a server with
 * no device (`--mock`) both answer "would …" with the reason, and whether
 * the server is a mock is settled BEFORE anything is sent: a question that
 * failed after the keys went out would report a failure for input that
 * happened, and invite the agent to send it twice.
 */
import {
  describeResult, LocateError, SIMULATED_NOTE, DRY_RUN_NOTE,
} from './locator.js'
import { untypableChars, untypableMessage } from './typing.js'

/**
 * Call a raw clawtouch-mcp tool and require it to say it SUCCEEDED.
 *
 * Every action tool answers `{ ok: true, ... }`. Treating "no error" as
 * success meant an unreadable reply — non-JSON text, an empty body, a
 * result whose `ok` never arrived — still produced "typed 12 characters".
 * Physical input either happened or it did not, and the agent has no other
 * way to find out.
 */
export async function mcpCall(locator, tool, args) {
  const res = await locator.mcp.callTool(tool, args)
  if (res.isError) throw new Error(`${tool} failed: ${res.text.slice(0, 300)}`)
  if (!res.json || res.json.ok !== true) {
    throw new Error(`${tool} did not confirm it ran: `
      + `${res.text.slice(0, 300) || 'empty reply'}`)
  }
  return res.json
}

/** "clicked" only when something really was clicked — a dry run that says
 *  it clicked is a lie the agent then reasons from. */
export function clickVerb(result) {
  return result && result.clicked ? 'clicked' : 'would click'
}

/** Click `target` first when one was given; the summary line it produced. */
async function clickFirst(locator, args) {
  if (!args.target) return []
  const clicked = await locator.click({ target: args.target, window: args.window })
  return [describeResult(clicked, clickVerb(clicked))]
}

/** computer_type. */
export async function typeText({ locator, config }, args) {
  // Before the target click as well as before the typing: text that cannot
  // be typed makes the whole call pointless, and a field clicked for nothing
  // is still a change on screen.
  const untypable = untypableChars(args.text)
  if (untypable.length) throw new LocateError(untypableMessage(untypable))
  const prefix = await clickFirst(locator, args)
  const length = args.text.length   // ASCII from here on: units are characters
  if (config.dryRun) {
    return { summary: [...prefix, `would type ${length} characters — ${DRY_RUN_NOTE}`].join('; ') }
  }
  const simulated = await locator.simulated()
  const res = await mcpCall(locator, 'hid.type', { text: args.text })
  const sent = Number.isInteger(res.chars) ? res.chars : length
  // The server drops control characters on purpose (a stray newline must not
  // submit a chat draft). Say so, or "typed 11 characters" for a 12-character
  // request leaves the agent guessing which one went missing.
  const dropped = length - sent
  const note = dropped > 0
    ? ` (${dropped} control character${dropped === 1 ? '' : 's'} such as a `
      + 'newline or tab left out — send Enter or Tab with computer_key)'
    : ''
  const line = simulated
    ? `would type ${sent} characters${note} — ${SIMULATED_NOTE}`
    : `typed ${sent} characters${note}`
  return { summary: [...prefix, line].join('; ') }
}

/** computer_key. */
export async function pressKey({ locator, config }, args) {
  const mods = Array.isArray(args.modifiers) ? args.modifiers : []
  const combo = [...mods, args.key].join('+')
  if (config.dryRun) return { summary: `would press ${combo} — ${DRY_RUN_NOTE}` }
  const simulated = await locator.simulated()
  await mcpCall(locator, 'hid.key', { key: args.key, modifiers: mods })
  return {
    summary: simulated ? `would press ${combo} — ${SIMULATED_NOTE}` : `pressed ${combo}`,
  }
}

/** computer_scroll. */
export async function scrollWheel({ locator, config }, args) {
  const prefix = await clickFirst(locator, args)
  if (config.dryRun) {
    return { summary: [...prefix, `would scroll ${args.amount} — ${DRY_RUN_NOTE}`].join('; ') }
  }
  const simulated = await locator.simulated()
  // The wire argument is `delta`, not `amount`. Naming it `amount` at the
  // agent-facing edge is fine; passing that name through was not — the
  // server rejected every call with a KeyError.
  await mcpCall(locator, 'hid.scroll', { delta: args.amount })
  const line = simulated
    ? `would scroll ${args.amount} — ${SIMULATED_NOTE}`
    : `scrolled ${args.amount}`
  return { summary: [...prefix, line].join('; ') }
}
