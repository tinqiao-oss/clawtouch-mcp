/**
 * The settings page's view of the plugin config: which settings it shows and
 * edits, how a save is written back into the profile, and which row is ours.
 *
 * Pure: no host, no device. index.js wires these into dsh's routes.
 *
 * The page edits four things — the eye (follow the conversation, or one dsh
 * route), dryRun, maxWidth, command — and nothing else. Everything else in
 * the row (vision.effort, vision.timeoutMs, args, env, port, the key-guard
 * switches...) is carried over untouched by a save, because dsh replaces a
 * row's config wholesale: a key the page dropped would be gone.
 *
 * What the page is told never includes a value it does not edit. In
 * particular the 0.1.x `vision.apiKey` is reported by NAME only (so the page
 * can offer to remove it), never by value.
 */
import { createHmac, randomBytes } from 'node:crypto'
import { isDeepStrictEqual } from 'node:util'
import { LEGACY_VISION_KEYS } from './eye.js'

export const DEFAULT_COMMAND = 'clawtouch-mcp'
export const DEFAULT_MAX_WIDTH = 1600
/** What the page accepts for maxWidth: below this a window's text is
 *  unreadable, above it no model looks at the extra pixels anyway. */
export const MAX_WIDTH_RANGE = Object.freeze([320, 7680])
const MAX_COMMAND_LENGTH = 1024
const MAX_ROUTE_PART = 256

const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)

/** The row's config as an object, whatever dsh handed over. */
const configOf = (raw) => (isObject(raw) ? raw : {})

/**
 * The settings the page shows, read the way apply() reads them.
 * `eye.mode` is 'conversation' (nothing set), 'route' (both set) or
 * 'incomplete' (only one of provider/model: the eye refuses that).
 */
export function editableSettings(raw) {
  const config = configOf(raw)
  const vision = isObject(config.vision) ? config.vision : {}
  const provider = typeof vision.provider === 'string' ? vision.provider : undefined
  const model = typeof vision.model === 'string' ? vision.model : undefined
  let eye
  if (provider && model) eye = { mode: 'route', provider, model }
  else if (provider || model) eye = { mode: 'incomplete', provider: provider ?? '', model: model ?? '' }
  else eye = { mode: 'conversation' }
  const width = Number(config.maxWidth)
  return {
    eye,
    dryRun: Boolean(config.dryRun),
    maxWidth: Number.isFinite(width) && width > 0 ? width : DEFAULT_MAX_WIDTH,
    command: typeof config.command === 'string' && config.command ? config.command : DEFAULT_COMMAND,
  }
}

/** The 0.1.x keys present in the row's vision block, by name only. */
export function legacyOf(raw) {
  const vision = configOf(raw).vision
  if (!isObject(vision)) return []
  return LEGACY_VISION_KEYS.filter((k) => Object.prototype.hasOwnProperty.call(vision, k))
}

// Keys the revision below to this process, so the value the page holds says
// nothing about the config it was computed from, not even to someone
// guessing what an API key might be.
const REVISION_KEY = randomBytes(32)

/** JSON with object keys sorted, so equal configs give equal text. */
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (isObject(value)) {
    return `{${Object.keys(value).sort()
      .filter((k) => value[k] !== undefined)
      .map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

/**
 * The revision a save must carry: an opaque token over everything a save
 * reads or rewrites — the three plain settings and the whole `vision`
 * block, the 0.1.x keys and their values included (an edit by hand to any
 * of them makes an open page stale, a key swapped in just before "remove
 * the old settings" too). Other keys are not in it: a save carries them
 * over as they are at the moment it writes.
 */
export function revisionOf(raw) {
  const config = configOf(raw)
  const relevant = {
    dryRun: config.dryRun, maxWidth: config.maxWidth, command: config.command, vision: config.vision,
  }
  return createHmac('sha256', REVISION_KEY).update(canonical(relevant)).digest('base64url')
}

/**
 * Whether every value the page reads or writes is a plain value. dsh hands
 * a `!!js` expression over as an object (`{ __jsExpr }`) and evaluates it
 * itself, so the page can neither show it truthfully nor overwrite it.
 */
export function plainSettings(raw) {
  const config = configOf(raw)
  if (Object.hasOwn(config, '__jsExpr')) return false
  const plain = {
    dryRun: (v) => typeof v === 'boolean',
    maxWidth: (v) => typeof v === 'number',
    command: (v) => typeof v === 'string',
  }
  if (!Object.entries(plain).every(([key, ok]) => config[key] === undefined || ok(config[key]))) {
    return false
  }
  if (config.vision === undefined) return true
  if (!isObject(config.vision) || Object.hasOwn(config.vision, '__jsExpr')) return false
  return ['provider', 'model'].every((k) => config.vision[k] === undefined || typeof config.vision[k] === 'string')
}

/**
 * What GET answers. `editable` is false (with the reason in `readOnly`)
 * when there is no row of ours to write, or a value is an expression.
 */
export function describeSettings(raw, { entryFound }) {
  const plain = plainSettings(raw)
  const shown = plain ? editableSettings(raw) : editableSettings({})
  return {
    ...shown,
    legacy: legacyOf(raw),
    revision: revisionOf(raw),
    editable: entryFound && plain,
    ...(entryFound ? (plain ? {} : { readOnly: 'expression' }) : { readOnly: 'editor' }),
  }
}

const routePart = (v) => typeof v === 'string' && v.trim() !== '' && v.length <= MAX_ROUTE_PART
  && v === v.trim() && !/[\u0000-\u001f]/.test(v)

/**
 * A save's body, or the name of the first field that is not acceptable.
 * Every field is required: the page always sends the whole form.
 */
export function parseSave(body) {
  if (!isObject(body)) return 'body'
  if (typeof body.revision !== 'string') return 'revision'
  const eye = body.eye
  if (!isObject(eye)) return 'eye'
  let parsedEye
  if (eye.mode === 'conversation') parsedEye = { mode: 'conversation' }
  else if (eye.mode === 'route') {
    if (!routePart(eye.provider)) return 'eye.provider'
    if (!routePart(eye.model)) return 'eye.model'
    parsedEye = { mode: 'route', provider: eye.provider, model: eye.model }
  } else return 'eye.mode'
  if (typeof body.dryRun !== 'boolean') return 'dryRun'
  const [lo, hi] = MAX_WIDTH_RANGE
  if (!Number.isInteger(body.maxWidth) || body.maxWidth < lo || body.maxWidth > hi) return 'maxWidth'
  if (typeof body.command !== 'string') return 'command'
  const command = body.command.trim()
  if (!command || command.length > MAX_COMMAND_LENGTH || /[\u0000-\u001f]/.test(command)) return 'command'
  if (body.dropLegacy !== undefined && typeof body.dropLegacy !== 'boolean') return 'dropLegacy'
  return {
    eye: parsedEye,
    dryRun: body.dryRun,
    maxWidth: body.maxWidth,
    command,
    dropLegacy: body.dropLegacy === true,
  }
}

const MANAGED = ['dryRun', 'maxWidth', 'command']

/**
 * The row's next config. Starts from the row as it is now (`current`) and
 * changes only what the page manages; the managed values are written only
 * where they differ from the plugin's own defaults, so a later default is
 * not masked by a pinned copy. A result that means the same as `inherited`
 * (the layers below the profile: the bundle's own defaults) returns
 * `inherited` itself, so dsh drops the profile's override of the row.
 */
export function withSettings(current, next, inherited) {
  const out = { ...configOf(current) }
  const set = (key, value, isDefault) => {
    if (isDefault) delete out[key]
    else out[key] = value
  }
  set('dryRun', true, !next.dryRun)
  set('maxWidth', next.maxWidth, next.maxWidth === DEFAULT_MAX_WIDTH)
  set('command', next.command, next.command === DEFAULT_COMMAND)
  // `command: python, args: [-m, clawtouch_mcp, ...]` is a valid way to start
  // the server; switched to the clawtouch-mcp program itself, that `-m
  // clawtouch_mcp` would reach its argument parser and stop it. Only those
  // two go; the server's own flags after them stay.
  const script = /(^|[\\/])clawtouch-mcp(\.exe)?$/i.test(next.command)
  if (script && Array.isArray(out.args) && out.args[0] === '-m'
      && /^clawtouch_mcp(\.__main__)?$/.test(String(out.args[1]))) {
    const rest = out.args.slice(2)
    if (rest.length) out.args = rest
    else delete out.args
  }

  const vision = isObject(out.vision) ? { ...out.vision } : {}
  delete vision.provider
  delete vision.model
  if (next.eye.mode === 'route') {
    vision.provider = next.eye.provider
    vision.model = next.eye.model
  }
  if (next.dropLegacy) for (const key of LEGACY_VISION_KEYS) delete vision[key]
  if (Object.keys(vision).length) out.vision = vision
  else delete out.vision

  const base = configOf(inherited)
  const others = (config) => {
    const rest = Object.fromEntries(Object.entries(config).filter(([key]) => !MANAGED.includes(key) && key !== 'vision'))
    const v = isObject(config.vision) ? { ...config.vision } : {}
    delete v.provider
    delete v.model
    return { rest, vision: v }
  }
  const meaning = (config) => JSON.stringify(editableSettings(config))
  if (plainSettings(base)
      && meaning(out) === meaning(base)
      && isDeepStrictEqual(others(out), others(base))) {
    return structuredClone(base)
  }
  return out
}

/**
 * This plugin's own row in the profile, the one the editor may write: the
 * entry whose fiber is this plugin's. Never matched by name — two rows of
 * one package are possible, and writing the other one's config would be
 * worse than not writing at all.
 */
export function ownEntry(editor, fiber) {
  try {
    if (fiber === undefined || typeof editor?.entries !== 'function') return undefined
    const mine = editor.entries().filter((entry) => holds(entry?.fiber, fiber))
    return mine.length === 1 ? mine[0] : undefined
  } catch {
    return undefined
  }
}

/**
 * Whether what a row keeps as its fiber is `fiber`. A row keeps either the
 * fiber itself (dsh 0.2.1's loader unwraps it) or what cordis' `plugin()`
 * hands back (dsh 0.2.2: an object made with the fiber as its prototype, so
 * that it can be awaited), whose `ctx.fiber` is the fiber. Learned the hard
 * way in dsh-engramory 1.5.1.
 */
export function holds(held, fiber) {
  return held !== undefined && held !== null && (held === fiber || held.ctx?.fiber === fiber)
}

/** The models the page offers, measured where we measured them. */
export function imageRoutes(models) {
  return models
    .filter((m) => Array.isArray(m.inputModalities) && m.inputModalities.includes('image'))
    .map((m) => ({ provider: m.provider, model: m.id, name: m.name || m.id }))
}
