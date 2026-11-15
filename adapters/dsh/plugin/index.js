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
// Also host-provided, same reasoning as above. Handed to lib/eye.js rather
// than imported there, so the lib/ modules stay testable with no host.
import { BlockAssembler } from '@deepseek-ai/dsh-llm'

import { McpStdioClient } from './lib/mcp-client.js'
import {
  Locator, describeResult, LocateError, TargetNotFound,
  renderWindowLine, notPressedNote, eyeNote,
} from './lib/locator.js'
import { refusedCombo } from './lib/keyguard.js'
import { DshEye } from './lib/eye.js'
import { clickVerb, typeText, pressKey, scrollWheel } from './lib/actions.js'
import { settingsRoute, statusRoute, modelsRoute, setupRoutes } from './lib/routes.js'
import { Installer, detect, MCP_VERSION } from './lib/installer.js'
import { ActivityGate } from './lib/activity.js'

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

// Which key combos are refused, and why, is in lib/keyguard.js (a pure
// function, so the rules are tested without a host).

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
  // The eye is a dsh model route (lib/eye.js). `llm` and `attachments` are
  // probed, not injected, for the same reason as `skills`: the tools that
  // need no looking (keys, typing without a target, the window list) must
  // still mount on a profile without them.
  const eye = new DshEye({ vision: config.vision, log })
  ctx.inject(['llm', 'attachments'], (inner) => {
    inner.effect(() => {
      eye.attach({ llm: inner.llm, attachments: inner.attachments, BlockAssembler })
      return () => eye.detach()
    })
  })
  const locator = new Locator({ mcp, config, log, eye })

  // Tool executions under way, and whether new ones may start (lib/activity.js):
  // a settings save and the page's device check both wait for none to run.
  const activity = new ActivityGate()
  // Installing clawtouch-mcp from the settings page (lib/installer.js); only
  // ever started by a click there.
  const installer = new Installer()

  // The device is not opened at boot. A Pico that is unplugged when the
  // profile starts is normal — the agent may plug it in mid-session, and
  // failing to mount over it would take the whole plugin (skill included)
  // down for a cable.
  //
  // Disposal (a reload after a settings save, or dsh shutting down) closes
  // both for good: an execution of this instance still waiting on the eye
  // must not restart clawtouch-mcp afterwards and press with old settings.
  //
  // An effect, not `ctx.on('dispose')`: Cordis 4 (dsh 0.2) has no dispose
  // event, so that listener never ran — every reload left the old
  // clawtouch-mcp holding the serial port (seen on dsh 0.2.2-rc.1: two
  // servers side by side after one save, the new one told the port was busy).
  ctx.effect(() => () => {
    activity.closeForGood('reloading')
    installer.close()
    mcp.close().catch(() => {})
  }, 'dsh-clawtouch: stop clawtouch-mcp')

  registerTools(ctx, locator, config, log, activity)
  registerGuard(ctx, config)
  registerSkill(ctx, config, log)
  registerPageRoutes(ctx, { config: userConfig, command: config.command, mcp, eye, activity, installer })
}

// ───────────────────────────── settings page ─────────────────────────────

// The bundle patch's row id: the one row that serves the page's routes.
const PAGE_ROW = 'clawtouch'

/**
 * The routes client.js calls (lib/routes.js). dsh's web server is optional (a
 * headless dsh has none) and may mount late, hence the probe. Only the
 * bundle's own row serves them: a second row of this package would otherwise
 * claim the same paths (dsh refuses a second claim) or, claiming them first,
 * have the page save ITS config. A row whose id cannot be told serves them.
 */
function registerPageRoutes(ctx, { config, command, mcp, eye, activity, installer }) {
  const fiber = ctx.fiber
  // `entry.id` is scoped by the tree that holds the row; the row's own id,
  // the one the patch names, is `options.id`.
  const rowId = fiber?.entry?.options?.id
  ctx.inject(['connection'], (inner) => {
    if (rowId !== undefined && rowId !== PAGE_ROW) return
    if (typeof inner.connection?.fetch?.register !== 'function') return
    const editor = () => inner.get('configEditor')
    const llm = () => eye.services?.llm
    for (const route of [
      settingsRoute({ editor, fiber, config, activity, installer }),
      statusRoute({ editor, fiber, config, llm, mcp, command, activity, installer }),
      modelsRoute({ llm }),
      ...setupRoutes({ installer, detect: () => detect(), mcp, activity, required: MCP_VERSION }),
    ]) {
      inner.effect(() => {
        try {
          return inner.connection.fetch.register(route)
        } catch {
          // a row of the same id in a nested include claimed the path first
          return () => {}
        }
      })
    }
  })
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
    // Alt+Tab, the Windows key, Cmd+Tab...: they move focus off the task
    // window, and every later real keystroke follows it. Refused unless the
    // machine being driven is not the one the agent runs on.
    allowFocusSwitchCombos: Boolean(raw.allowFocusSwitchCombos),
    registerSkill: raw.registerSkill !== false,
    // Which dsh model route looks (lib/eye.js). Unset = the conversation's
    // own model. The 0.1.x keys (endpoint, apiKey, extraBody) are passed
    // through untouched so the eye can refuse them by name — an old profile
    // must not silently start using a different model. Environment
    // variables are not read: a stray DASHSCOPE_API_KEY decides nothing.
    vision: { ...vision },
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

function registerTools(ctx, locator, config, log, activity) {
  const run = async (fn) => {
    try {
      return await activity.run(fn)
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
      + 'model finds it and a USB HID mouse clicks it — a real click, with '
      + 'no undo. You will not see the '
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
    execute: (args, exec) => run(async () => {
      const result = await locator.click({
        target: args.target,
        window: args.window,
        button: args.button,
        double: args.double,
        signal: exec?.signal,
        exec,
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
    execute: (args, exec) => run(async () => {
      const result = await locator.clickSequence({
        targets: args.targets,
        window: args.window,
        button: args.button,
        signal: exec?.signal,
        exec,
      })
      const verb = result.clicked ? 'clicked' : 'would click'
      const where = result.results
        .map((r) => `${JSON.stringify(r.target)} (${r.screen[0]}, ${r.screen[1]})`)
        .join(' -> ')
      const note = notPressedNote(result)
      const summary = `${verb} ${result.results.length} in ${result.source}: `
        + `${where} [${result.timings.totalMs}ms for the look${eyeNote(result.eye)}]`
        + (note ? ` — ${note}` : '')
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
    execute: (args, exec) => run(async () => {
      let result
      try {
        result = await locator.point({
          target: args.target, window: args.window,
          signal: exec?.signal, exec,
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
      + 'almost always what you want. Plain ASCII only: the device types one '
      + 'key per character on a US layout, so text with Chinese, emoji or '
      + 'curly quotes is refused before anything is typed.',
    parameters: {
      text: {
        type: 'string',
        required: true,
        description: 'The literal text to type, in plain ASCII.',
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
    // In lib/actions.js, with key and scroll, so the tests reach them.
    execute: (args, exec) => run(() => typeText({ locator, config }, args, exec)),
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
    execute: (args, exec) => run(() => pressKey({ locator, config }, args, exec)),
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
    execute: (args, exec) => run(() => scrollWheel({ locator, config }, args, exec)),
  }))
}

// ───────────────────────────── guard ─────────────────────────────

function registerGuard(ctx, config) {
  const quit = !config.allowQuitCombos
  const focus = !config.allowFocusSwitchCombos
  if (!quit && !focus) return
  ctx.tools.guard((exec) => {
    if (exec.name !== 'computer_key') return undefined
    const args = exec.arguments ?? {}
    const hit = refusedCombo(args.key, args.modifiers, { quit, focus })
    if (!hit) return undefined
    if (hit.kind === 'quit') {
      return `${hit.what} is blocked: a USB HID keystroke lands on `
        + 'whatever window has focus, so if that is this agent\'s own '
        + 'window the combo ends this session mid-task. Close the target '
        + 'another way (click its close button), or set '
        + '`allowQuitCombos: true` in the plugin config if the machine '
        + 'being driven is not this one.'
    }
    // Raising a covered window is Windows-only (see the platform table in
    // the README), so the way round differs.
    const wayRound = process.platform === 'win32'
      ? 'To work in a different window, name it: '
        + '`computer_click({ window: "<title>", target })` brings that window '
        + 'forward by its title bar.'
      : 'To work in a different window, click a part of it that is visible. '
        + 'This plugin cannot bring a covered window forward on this platform.'
    return `${hit.what} is blocked: it moves focus to another window, and `
      + 'a USB HID keystroke goes wherever focus is — every later key, and '
      + 'every "type into the focused field", would land there instead of '
      + `the task window. ${wayRound} Set \`allowFocusSwitchCombos: true\` in `
      + 'the plugin config if the machine being driven is not this one.'
  })
}

// ───────────────────────────── skill ─────────────────────────────

const SKILL = `# Driving a real computer

You can operate this machine's real screen through a USB HID mouse and
keyboard. What you click and type happens on the actual screen.

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
- Text is typed one key per character on a US keyboard layout. Chinese,
  emoji and curly quotes have no key there, so \`computer_type\` refuses such
  text before typing any of it. If a Chinese input method is switched on,
  typed letters can turn into candidates and punctuation into full-width
  forms — check the field before relying on what was typed.
- A reply that says "would click" or "nothing was pressed" means no input
  happened (a dry run, or a server with no device). Do not carry on as if
  it had.
- Quit combos (Cmd+Q, Alt+F4, Cmd+W, Alt+Space) are blocked by default — on a shared
  machine they would close this session.
- So are window-switching combos (Alt+Tab, the Windows key, Cmd+Tab). They
  move focus away and every later keystroke follows it. To reach another
  window, name it: \`computer_click({ window: "<title>", target })\` (on
  Windows this brings it forward; elsewhere click a visible part of it).
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
