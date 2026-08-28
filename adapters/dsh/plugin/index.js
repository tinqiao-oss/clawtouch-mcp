/**
 * dsh-clawtouch — real mouse and keyboard for a DeepSeek Harness agent,
 * driven by natural language instead of coordinates.
 *
 * The agent says `computer_click({ target: "the Send button" })`. It never
 * receives a screenshot, never computes a coordinate, and never learns
 * what resolution anything is. Inside one tool call this plugin crops the
 * target window, stamps two calibration markers, asks a vision model for
 * the markers and the target together, solves the model's unreported
 * internal rescale from the markers, and sends the click through a real
 * USB HID device.
 *
 * **Why this is a plugin and not just the MCP server.** `clawtouch-mcp` is
 * deliberately raw HID plumbing — no LLM, no agent loop (see its README).
 * Three things therefore cannot live in it and are exactly what this
 * package adds:
 *
 *   1. A *second model*. The model that runs the tool loop and the model
 *      that points at pixels are not the same model today; this layer
 *      routes the "where is it" question to a vision model and keeps the
 *      answer out of the agent's context.
 *   2. A *skill*, so the workflow is present because the plugin loaded,
 *      not because a file landed in the right one of five skill roots.
 *   3. A *guard*. `ctx.tools.guard()` is synchronous and monotonic —
 *      returning a reason denies the call and no later listener can undo
 *      it. That is a stronger seam than a warning, which is all the MCP
 *      server can offer for a keystroke that would quit the agent driving
 *      it.
 *
 * Plain ESM. `@deepseek-ai/dsh-tools` is a peer dependency — every dsh
 * profile already has it, and using the host's own `defineTool` is what
 * keeps the tool schemas valid against the host that will run them.
 */
// Provided by the host: a dsh plugin runs inside dsh, which owns this
// package. It is deliberately NOT declared as a peerDependency, and that
// is not an oversight — while dsh ships release candidates, semver's
// prerelease rule makes the declaration actively harmful: a prerelease
// version only satisfies a range that itself carries a prerelease with the
// same major.minor.patch, so NO range matches the installed 0.1.1-rc.2 —
// not `>=0.1.0`, not `>=0.0.1-rc.1`, not even `*`. Measured: with the peer
// declared, installing the plugin into a profile that already has
// dsh-tools@0.1.1-rc.2 is refused with ERESOLVE (and one variant had npm
// remove the host's copy); with it gone, the install is clean and this
// import resolves to the host's own copy. The requirement is documented in
// the README instead, where it cannot break resolution.
import { defineTool } from '@deepseek-ai/dsh-tools'

import { McpStdioClient } from './lib/mcp-client.js'
import {
  Locator, describeResult, LocateError, TargetNotFound,
  renderWindowLine,
} from './lib/locator.js'

export const name = 'clawtouch'

// `tools` is the only hard dependency. `skills` is deliberately NOT
// declared: the input tools must mount on a profile that has no skill
// registry at all, so apply() probes for it at runtime instead. Cordis
// reads `inject` as a flat array of service names and waits for every one
// of them — declaring an optional service here would leave the plugin
// pending forever on profiles that never mount it.
export const inject = ['tools']

const DEFAULT_COMMAND = 'clawtouch-mcp'
const DEFAULT_MAX_WIDTH = 1600

/** Key combos that would quit the agent driving the keyboard. Real USB
 *  HID has no app-level addressing: the keystroke lands wherever focus
 *  is, and if that is the dsh window, the session ends mid-task. */
const QUIT_COMBOS = [
  { key: 'q', mods: ['gui', 'cmd', 'win'], what: 'Cmd+Q (quit application)' },
  { key: 'f4', mods: ['alt'], what: 'Alt+F4 (close window)' },
  { key: 'w', mods: ['gui', 'cmd', 'win'], what: 'Cmd+W (close window)' },
]

export function apply(ctx, userConfig = {}) {
  const config = normalize(userConfig)
  const log = (level, msg) => {
    const logger = ctx.logger?.('clawtouch')
    if (logger && typeof logger[level] === 'function') logger[level](msg)
  }

  const mcp = new McpStdioClient({
    command: config.command,
    args: config.args,
    env: config.env,
    cwd: config.cwd,
    log,
  })
  const locator = new Locator({ mcp, config, log })

  // The device is not opened at boot. A Pico that is unplugged when the
  // profile starts is normal — the agent may plug it in mid-session, and
  // failing to mount over it would take the whole plugin (skill included)
  // down for a cable.
  ctx.on('dispose', () => { mcp.stop().catch(() => {}) })

  registerTools(ctx, locator, config, log)
  registerGuard(ctx, config)
  registerSkill(ctx, config, log)
}

// ───────────────────────────── config ─────────────────────────────

function normalize(raw) {
  const vision = raw.vision ?? {}
  const args = Array.isArray(raw.args) ? [...raw.args] : []
  // Both screen tools are gated behind one flag in clawtouch-mcp, and
  // without it this plugin has nothing to work with — a missing flag
  // would surface as "tool not found" three calls later.
  if (raw.allowScreenshot !== false && !args.includes('--allow-screenshot')) {
    args.push('--allow-screenshot')
  }
  if (raw.mock && !args.includes('--mock')) args.push('--mock')
  if (raw.port && !args.includes('--port')) args.push('--port', String(raw.port))
  return {
    command: raw.command || DEFAULT_COMMAND,
    args,
    env: raw.env,
    cwd: raw.cwd,
    maxWidth: positive(raw.maxWidth, DEFAULT_MAX_WIDTH),
    imageFormat: raw.imageFormat === 'png' ? 'png' : 'jpeg',
    dryRun: Boolean(raw.dryRun),
    // Bring a background or partly-covered window forward before looking
    // at it, by clicking its title bar with the real mouse. On by default
    // because the alternative is refusing to work on any window that is
    // not already focused; set false where the agent must never change
    // which window the person is looking at.
    autoRaise: raw.autoRaise !== false,
    allowQuitCombos: Boolean(raw.allowQuitCombos),
    registerSkill: raw.registerSkill !== false,
    vision: {
      endpoint: vision.endpoint,
      model: vision.model,
      // An env var is the default so a key never has to be written into a
      // profile file that gets committed by accident.
      apiKey: vision.apiKey || process.env.DASHSCOPE_API_KEY
        || process.env.CLAWTOUCH_VISION_API_KEY,
      timeoutMs: positive(vision.timeoutMs, 0) || undefined,
    },
  }
}

function positive(value, fallback) {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

// ───────────────────────────── tools ─────────────────────────────

const TARGET_PARAM = {
  type: 'string',
  required: true,
  description:
    'What to click, in plain language, as a person would point it out: '
    + '"the blue Send button", "the search box at the top", "the third '
    + 'row in the list". Describe what it LOOKS like and where it sits — '
    + 'never a coordinate.',
}

const WINDOW_PARAM = {
  type: 'string',
  description:
    'Case-insensitive part of the target window title. Omit to use the '
    + 'window currently in front — or, where the OS could not be asked '
    + 'which one that is, the first one listed, which the answer says so '
    + 'in as many words. Cropping to one window is what keeps the '
    + 'location accurate on a large or multi-monitor desktop — call '
    + 'computer_windows first if unsure of the title.',
}

function registerTools(ctx, locator, config, log) {
  const run = async (fn) => {
    try {
      return await fn()
    } catch (err) {
      // A LocateError is an expected outcome (target not on screen, the
      // model refused, the device is unplugged) and reads better to the
      // agent as a sentence than as a stack trace.
      if (err instanceof LocateError) throw new Error(err.message)
      throw err
    }
  }

  ctx.tools.register(defineTool({
    name: 'computer_click',
    description:
      'Click something on the real screen, described in words. A vision '
      + 'model finds it and a USB HID device performs the click, so the '
      + 'target OS sees a genuine physical mouse. You will not see the '
      + 'screen: say what you want clicked and read back what happened. '
      + 'If the reply says the target was not found, look at the window '
      + 'list or describe the element differently rather than retrying '
      + 'the same words.',
    parameters: {
      target: TARGET_PARAM,
      window: WINDOW_PARAM,
      button: {
        type: 'string',
        enum: ['left', 'right', 'middle'],
        description: 'Mouse button. Default left.',
      },
      double: {
        type: 'boolean',
        description: 'Double-click instead of a single click.',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          clicked: { type: 'boolean', required: true },
          summary: { type: 'string', required: true },
          x: { type: 'integer', required: true },
          y: { type: 'integer', required: true },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.summary }],
    },
    execute: (args) => run(async () => {
      const result = await locator.click({
        target: args.target,
        window: args.window,
        button: args.button,
        double: args.double,
      })
      const verb = clickVerb(result)
      const summary = describeResult(result, verb)
      log('info', summary)
      return {
        clicked: Boolean(result.clicked),
        summary,
        x: result.screen[0],
        y: result.screen[1],
      }
    }),
  }))

  ctx.tools.register(defineTool({
    name: 'computer_click_sequence',
    description:
      'Click several things in order, from ONE look at the screen. Much '
      + 'faster than calling computer_click repeatedly — one screenshot, '
      + 'one look, one batch of clicks — but only correct when every '
      + 'target is visible AT THE SAME TIME and clicking them does not '
      + 'move the others. Calculator keys, a row of toolbar buttons, a '
      + 'checkbox and then Save: fine. Anything that opens, closes, '
      + 'scrolls or navigates: use computer_click one at a time, looking '
      + 'again in between. If any target is not found, NOTHING is '
      + 'clicked — a half-finished sequence is harder to recover from '
      + 'than one that never started.',
    parameters: {
      targets: {
        type: 'array',
        required: true,
        description:
          'What to click, in order, each described the way you would '
          + 'describe it to a person. Up to 8.',
        items: { type: 'string' },
      },
      window: WINDOW_PARAM,
      button: {
        type: 'string',
        enum: ['left', 'right', 'middle'],
        description: 'Mouse button for every click. Default left.',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          clicked: { type: 'boolean', required: true },
          count: { type: 'integer', required: true },
          summary: { type: 'string', required: true },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.summary }],
    },
    execute: (args) => run(async () => {
      const result = await locator.clickSequence({
        targets: args.targets,
        window: args.window,
        button: args.button,
      })
      const verb = result.clicked ? 'clicked' : 'would click'
      const where = result.results
        .map((r) => `${JSON.stringify(r.target)} (${r.screen[0]}, ${r.screen[1]})`)
        .join(' -> ')
      const summary = `${verb} ${result.results.length} in ${result.source}: `
        + `${where} [${result.timings.totalMs}ms for the look]`
      log('info', summary)
      return {
        clicked: Boolean(result.clicked),
        count: result.results.length,
        summary,
      }
    }),
  }))

  ctx.tools.register(defineTool({
    name: 'computer_find',
    description:
      'Locate something on the real screen WITHOUT clicking it. Use it to '
      + 'check whether an element is on screen before acting, or to '
      + 'confirm an action landed. Costs one vision call, same as a click.',
    parameters: { target: TARGET_PARAM, window: WINDOW_PARAM },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          found: { type: 'boolean', required: true },
          summary: { type: 'string', required: true },
          // Optional, because a coordinate is exactly what this
          // tool does NOT have when it did not find anything.
          // Required forced a stand-in, and the stand-in was
          // (-1, -1) — a pair of specific integers standing for
          // "no answer", which is the same substitution this
          // plugin refuses everywhere else.
          x: { type: 'integer' },
          y: { type: 'integer' },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.summary }],
    },
    execute: (args) => run(async () => {
      let result
      try {
        result = await locator.point({
          target: args.target, window: args.window,
        })
      } catch (err) {
        // "Not on screen" is this tool's whole purpose — an answer, not a
        // failure. Only a caller about to click has to treat it as one.
        if (err instanceof TargetNotFound) {
          // No x/y at all: absent is the answer, not (-1, -1).
          return { found: false, summary: err.message }
        }
        throw err
      }
      return {
        found: true,
        summary: describeResult(result, 'found'),
        x: result.screen[0],
        y: result.screen[1],
      }
    }),
  }))

  ctx.tools.register(defineTool({
    name: 'computer_windows',
    description:
      'List the visible windows on the real screen with their titles. Use '
      + 'it to learn what is open and to get the exact title to pass as '
      + '`window` to the other computer_* tools.',
    parameters: {},
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          windows: {
            type: 'array',
            required: true,
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                title: { type: 'string', required: true },
                width: { type: 'integer', required: true },
                height: { type: 'integer', required: true },
                // Every field below is reported ONLY where it was
                // measured — see the support table in the README. Absent
                // means "not measured", never "measured and fine".
                //
                // `foreground` joined them: it is a real query on both
                // platforms, but a query can fail, and answering False
                // for "nobody was asked" states that this window is not
                // in front, which is not what was found out.
                foreground: { type: 'boolean' },
                visible_percent: { type: 'integer' },
                accepts_input: { type: 'boolean' },
              },
            },
          },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        // One line per window, each naming the answers it does NOT
        // carry — see renderWindowLine in lib/locator.js, which is where
        // it lives so the tests can reach it without this file's
        // `@deepseek-ai/dsh-tools` import.
        text: value.windows.length
          ? value.windows.map(renderWindowLine).join('\n')
          : 'no visible windows',
      }],
    },
    execute: () => run(async () => {
      const wins = await locator.windows()
      return {
        windows: wins.map((w) => {
          const out = {
            title: String(w.title ?? ''),
            width: w.rect[2] - w.rect[0],
            height: w.rect[3] - w.rect[1],
          }
          // NOT `Boolean(w.foreground)`: that turned a missing answer
          // into `false`, which is the exact substitution this whole
          // tool exists to avoid.
          if (typeof w.foreground === 'boolean') out.foreground = w.foreground
          // Both fields below are reported ONLY where they were measured.
          // Where they were not, they are left out: filling in 100 / true
          // would hand the agent a guard result that nothing ever checked,
          // which is worse than no answer because it reads as "looked, and
          // it is fine". Windows measures both; the other platforms do not
          // (see the support table in the README).

          // How much of it is actually on top. A window at 0% is fully
          // behind another one, and a screenshot of its rectangle would
          // be a screenshot of that other window.
          if (typeof w.visible_fraction === 'number') {
            out.visible_percent = Math.round(w.visible_fraction * 100)
          }
          // False while a modal dialog owns it: the window looks normal
          // and screenshots normally, but discards every click.
          if (typeof w.enabled === 'boolean') out.accepts_input = w.enabled
          return out
        }),
      }
    }),
  }))

  ctx.tools.register(defineTool({
    name: 'computer_type',
    description:
      'Type text on the real keyboard. Keystrokes go wherever the focus '
      + 'already is — pass `target` to click a field first, which is '
      + 'almost always what you want.',
    parameters: {
      text: {
        type: 'string',
        required: true,
        description: 'The literal text to type.',
      },
      target: {
        type: 'string',
        description:
          'Optional: click this first (same description style as '
          + 'computer_click) so the text lands in the right field.',
      },
      window: WINDOW_PARAM,
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: { summary: { type: 'string', required: true } },
      },
      render: (_args, value) => [{ type: 'text', text: value.summary }],
    },
    execute: (args) => run(async () => {
      const prefix = []
      if (args.target) {
        const clicked = await locator.click({
          target: args.target, window: args.window,
        })
        prefix.push(describeResult(clicked, clickVerb(clicked)))
      }
      if (config.dryRun) {
        return { summary: [...prefix, `would type ${args.text.length} chars`].join('; ') }
      }
      const res = await mcpCall(locator, 'hid.type', { text: args.text })
      return {
        summary: [...prefix,
          `typed ${res.chars ?? args.text.length} characters`,
        ].join('; '),
      }
    }),
  }))

  ctx.tools.register(defineTool({
    name: 'computer_key',
    description:
      'Press a key or key combination on the real keyboard, e.g. Enter, '
      + 'Tab, Escape, or Ctrl+C. Goes to whatever window has focus.',
    parameters: {
      key: {
        type: 'string',
        required: true,
        description: 'Key name: enter, tab, escape, f5, a, 1, ...',
      },
      modifiers: {
        type: 'array',
        items: { type: 'string' },
        description: 'Held with the key: ctrl, shift, alt, gui (Win/Cmd).',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: { summary: { type: 'string', required: true } },
      },
      render: (_args, value) => [{ type: 'text', text: value.summary }],
    },
    execute: (args) => run(async () => {
      const mods = Array.isArray(args.modifiers) ? args.modifiers : []
      const combo = [...mods, args.key].join('+')
      if (config.dryRun) return { summary: `would press ${combo}` }
      await mcpCall(locator, 'hid.key', { key: args.key, modifiers: mods })
      return { summary: `pressed ${combo}` }
    }),
  }))

  ctx.tools.register(defineTool({
    name: 'computer_scroll',
    description:
      'Scroll the real mouse wheel. Positive scrolls up, negative down. '
      + 'Pass `target` to move the pointer over the right pane first — '
      + 'scrolling applies to whatever is under the cursor.',
    parameters: {
      amount: {
        type: 'integer',
        required: true,
        description: 'Wheel clicks. Positive = up, negative = down.',
      },
      target: {
        type: 'string',
        description: 'Optional: click here first to put the cursor in the '
          + 'pane you mean to scroll.',
      },
      window: WINDOW_PARAM,
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: { summary: { type: 'string', required: true } },
      },
      render: (_args, value) => [{ type: 'text', text: value.summary }],
    },
    execute: (args) => run(async () => {
      const prefix = []
      if (args.target) {
        const clicked = await locator.click({
          target: args.target, window: args.window,
        })
        prefix.push(describeResult(clicked, clickVerb(clicked)))
      }
      if (config.dryRun) {
        return { summary: [...prefix, `would scroll ${args.amount}`].join('; ') }
      }
      // The wire argument is `delta`, not `amount`. Naming it `amount` at
      // the agent-facing edge is fine; passing that name through was not
      // — the server rejected every call with a KeyError.
      await mcpCall(locator, 'hid.scroll', { delta: args.amount })
      return {
        summary: [...prefix, `scrolled ${args.amount}`].join('; '),
      }
    }),
  }))
}

/**
 * Call a raw clawtouch-mcp tool and require it to say it SUCCEEDED.
 *
 * Every action tool answers `{ ok: true, ... }`. Treating "no error" as
 * success meant an unreadable reply — non-JSON text, an empty body, a
 * result whose `ok` never arrived — still produced "typed 12 characters".
 * Physical input either happened or it did not, and the agent has no other
 * way to find out.
 */
async function mcpCall(locator, tool, args) {
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
function clickVerb(result) {
  return result && result.clicked ? 'clicked' : 'would click'
}

// ───────────────────────────── guard ─────────────────────────────

function registerGuard(ctx, config) {
  if (config.allowQuitCombos) return
  ctx.tools.guard((exec) => {
    if (exec.name !== 'computer_key') return undefined
    const args = exec.arguments ?? {}
    const key = String(args.key ?? '').trim().toLowerCase()
    const mods = new Set((Array.isArray(args.modifiers) ? args.modifiers : [])
      .map((m) => String(m).toLowerCase()))
    for (const combo of QUIT_COMBOS) {
      if (key !== combo.key) continue
      if (!combo.mods.some((m) => mods.has(m))) continue
      return `${combo.what} is blocked: a USB HID keystroke lands on `
        + 'whatever window has focus, so if that is this agent\'s own '
        + 'window the combo ends this session mid-task. Close the target '
        + 'another way (click its close button), or set '
        + '`allowQuitCombos: true` in the plugin config if the machine '
        + 'being driven is not this one.'
    }
    return undefined
  })
}

// ───────────────────────────── skill ─────────────────────────────

const SKILL = `# Driving a real computer

You can operate a real screen through a USB HID device. The keyboard and
mouse are physical: the target OS cannot tell them from a person's.

## What you can and cannot see

You never receive a screenshot. \`computer_click\` and \`computer_find\`
take a **description in words** and a vision model does the looking:

    computer_click({ target: "the blue Send button at the bottom right" })

Describe appearance and position, the way you would point something out to
a person across the room. Coordinates are not yours to supply and are
never useful here.

## The loop that works

1. \`computer_windows\` — see what is open, get an exact title.
2. \`computer_click({ target, window })\` — always pass \`window\` when you
   know it. Cropping to one window is what keeps the location accurate;
   without it, a wide desktop is downscaled until small text is
   unreadable.
3. Read the reply. It tells you what was clicked and where.
4. \`computer_find\` to confirm the screen changed as expected before
   doing anything destructive.

## When several clicks are all on screen at once

Use \`computer_click_sequence({ targets: [...] })\`. It looks once and
clicks them all, which is several times faster than one call each — the
looking is the slow part, and so is your own turn between calls.

The condition is simple and you must actually check it: **every target has
to be visible at the same time, and clicking them must not move the
others.** Typing a number into a calculator, ticking three boxes on a
form, clicking a tool then a colour — those qualify. Opening something,
navigating, scrolling, or anything where the second target only appears
after the first click does not; use \`computer_click\` for those and look
again in between.

## Windows that are not in front

You do not normally have to think about this: naming a \`window\` brings it
forward first, by clicking its title bar with the real mouse, exactly as a
person would.

It gives up and tells you when it cannot — a window with no title bar to
grab, or one that a modal dialog has disabled (that one looks completely
normal in a screenshot and silently discards every click, so it is worth
recognising). Deal with the dialog, or pick a different route.

## When it says the target was not found

That is an honest answer, not a transient failure. Retrying the same
words gets the same result. Instead: check \`computer_windows\` (is the
right window even open?), or describe the element differently — by
colour, by the text on it, by what it sits next to.

## What to be careful about

- Keystrokes go to whatever window has focus. Click the field first, or
  pass \`target\` to \`computer_type\`, which does it for you.
- Quit combos (Cmd+Q, Alt+F4, Cmd+W) are blocked by default — on a shared
  machine they would close this session.
- Every click is real and immediate. There is no undo. Before anything
  irreversible — sending a message, confirming a payment, deleting — use
  \`computer_find\` to verify you are pointing at what you think.
`

function registerSkill(ctx, config, log) {
  if (!config.registerSkill) return
  // Probed, not injected: the input tools must still mount on a profile
  // with no skill registry. Cordis's reactive inject waits for a service
  // that may never arrive, so this asks once the plugin is already live.
  ctx.inject(['skills'], (inner) => {
    inner.effect(() => inner.skills.register({
      name: 'clawtouch-computer-use',
      description:
        'Operate a real screen through a USB HID mouse and keyboard by '
        + 'describing what to click in words; a vision model does the '
        + 'looking and no screenshot enters the conversation.',
      whenToUse:
        'Any task that means acting on the machine\'s actual screen — a '
        + 'desktop app with no API, a page that must be clicked, anything '
        + 'where "do it in the UI" is the only route.',
      source: 'runtime',
      content: SKILL,
    }))
    log('info', 'clawtouch skill registered')
  })
}
