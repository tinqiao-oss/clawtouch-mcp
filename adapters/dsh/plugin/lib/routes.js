/**
 * The settings page's routes, served by dsh's web server for the page in
 * client.js. dsh answers an exact route for the owner alone (a signed-in
 * browser, or a program holding the owner's login: no cookie 401, cross-site
 * 403), so an agent cannot call these to change what the plugin presses with.
 *
 *   GET  /api/clawtouch/settings  what the form shows, and whether it can save
 *   POST /api/clawtouch/settings  save it through dsh's profile editor
 *   POST /api/clawtouch/status    check the eye and the device, pressing nothing
 *   GET  /api/clawtouch/models    the dsh routes that declare image input
 *
 * What leaves here is never raw configuration or an exception's text from
 * reading it: dsh's YAML errors quote the lines around the fault, and those
 * can be the 0.1.x `vision.apiKey`. Failures are fixed codes. The device
 * check does pass on clawtouch-mcp's own diagnostics (port names, firmware
 * errors), which never see the profile.
 *
 * Built from injected pieces (the config editor, the llm service, the MCP
 * client, the activity gate) so the tests drive them with stand-ins.
 */
import {
  describeSettings, parseSave, withSettings, ownEntry, revisionOf, plainSettings,
  editableSettings, legacyOf, imageRoutes,
} from './settings.js'

export const SETTINGS_ROUTE = '/api/clawtouch/settings'
export const STATUS_ROUTE = '/api/clawtouch/status'
export const MODELS_ROUTE = '/api/clawtouch/models'

/** How long the status check waits on each part. The page shows what it got. */
const EYE_CHECK_MS = 5_000
const DEVICE_CHECK_MS = 8_000
const MODELS_PER_PROVIDER_MS = 5_000
/** A model list this fresh is answered again instead of asked again. */
const MODELS_FRESH_MS = 30_000
/** After a save dsh reloads the plugin, which closes this instance for good;
 *  if no reload follows (nothing changed after all), tools reopen after this. */
const RELOAD_GRACE_MS = 5_000
/** Longest diagnostic passed on from clawtouch-mcp. */
const DETAIL_MAX = 300

/** Every answer: JSON, never cached, never sniffed as anything else. */
export const json = (status, body) => new Response(JSON.stringify(body), {
  status,
  headers: {
    'content-type': 'application/json',
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
  },
})

/** Settle `promise` or give up after `ms`, aborting `controller` if given. */
async function within(promise, ms, controller) {
  let timer
  const late = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller?.abort()
      reject(Object.assign(new Error(`no answer within ${ms} ms`), { timeout: true }))
    }, ms)
  })
  try {
    return await Promise.race([promise, late])
  } finally {
    clearTimeout(timer)
  }
}

const detail = (text) => (text ? String(text).slice(0, DETAIL_MAX) : undefined)

/**
 * @param {object} deps
 * @param {() => object | undefined} deps.editor   dsh's configEditor, if mounted
 * @param {object} deps.fiber                      this plugin's fiber (ctx.fiber)
 * @param {object} deps.config                     the config apply() was given
 * @param {import('./activity.js').ActivityGate} deps.activity
 */
export function settingsRoute({ editor, fiber, config, activity, graceMs = RELOAD_GRACE_MS }) {
  const read = () => {
    const entry = ownEntry(editor(), fiber)
    const raw = entry?.options?.config ?? config
    return json(200, describeSettings(raw, { entryFound: entry !== undefined }))
  }
  const write = async (request) => {
    let body
    try {
      body = await request.json()
    } catch {
      return json(400, { ok: false, error: 'invalid', field: 'body' })
    }
    const next = parseSave(body)
    if (typeof next === 'string') return json(400, { ok: false, error: 'invalid', field: next })
    const ed = editor()
    const entry = ownEntry(ed, fiber)
    if (entry === undefined) return json(409, { ok: false, error: 'unavailable' })
    if (!plainSettings(entry.options?.config)) return json(409, { ok: false, error: 'expression' })
    const stale = new Error('stale')
    const expression = new Error('expression')
    try {
      await ed.edit(entry, (current, inherited) => {
        // The editor reads the profile afresh: a `!!js` value written there
        // since the check above, or any edit by hand, must not be overwritten.
        if (!plainSettings(current)) throw expression
        if (revisionOf(current) !== body.revision) throw stale
        return withSettings(current, next, inherited)
      })
    } catch (error) {
      if (error === stale) return json(409, { ok: false, error: 'stale' })
      if (error === expression) return json(409, { ok: false, error: 'expression' })
      // dsh refuses a value a home patch or a command-line overlay would
      // override anyway. Anything else is reported as a code: the message
      // can quote the profile, secrets and all.
      const overridden = /overridden/i.test(String(error?.message ?? ''))
      return json(overridden ? 409 : 500, { ok: false, error: overridden ? 'overridden' : 'failed' })
    }
    return json(200, { ok: true })
  }
  const save = async (request) => {
    // Closed before the first await: an execution must not start between
    // this check and the reload, nor an old one go on after it.
    const reopen = activity.closeIfIdle('saving its settings')
    if (!reopen) return json(409, { ok: false, error: 'busy' })
    let response
    try {
      response = await write(request)
      return response
    } finally {
      if (response?.status === 200) {
        const timer = setTimeout(reopen, graceMs)
        timer.unref?.()
      } else {
        reopen()
      }
    }
  }
  // One route for both methods: dsh registers a path once, and refuses a second.
  return {
    path: SETTINGS_ROUTE,
    methods: ['GET', 'POST'],
    requestBody: 'buffered',
    fetch: (request) => (request.method === 'POST' ? save(request) : read()),
  }
}

/**
 * What the eye will be, without asking it anything: the route the config
 * names, and whether dsh says that route takes images. Following the
 * conversation can only be judged per conversation, so it is reported as
 * such.
 */
export async function checkEye(raw, llm, ms = EYE_CHECK_MS) {
  const legacy = legacyOf(raw)
  const { eye } = editableSettings(plainSettings(raw) ? raw : {})
  const out = { ...eye, legacy }
  if (eye.mode !== 'route') return out
  if (typeof llm?.resolveModelInfo !== 'function') return { ...out, image: null, problem: 'no-llm' }
  const controller = new AbortController()
  try {
    const info = await within(llm.resolveModelInfo(eye.provider, eye.model, controller.signal), ms, controller)
    const modalities = info?.inputModalities
    return { ...out, image: Array.isArray(modalities) ? modalities.includes('image') : null }
  } catch (error) {
    return { ...out, image: null, problem: error?.timeout ? 'timeout' : 'failed' }
  }
}

/**
 * The device, through the plugin's own clawtouch-mcp process — never a
 * second one: the serial port is exclusive. Skipped while a tool execution
 * runs (or a save is under way): clawtouch-mcp answers one request at a
 * time, and the check must not land between a capture and its click.
 */
export async function checkDevice(mcp, command, activity, ms = DEVICE_CHECK_MS) {
  const busy = { mcp: serverOf(mcp, command), board: { state: 'busy' } }
  if (!activity.idle) return busy
  const starting = mcp.start()
  try {
    await within(starting, ms)
  } catch (error) {
    if (error?.timeout) {
      // The start goes on without us. A server whose only request is the
      // handshake never starts its idle release and would hold the port
      // until the next tool call; device.info (no I/O, in every version)
      // starts it. Not while a tool runs: its own call does the same.
      starting.then(() => {
        if (activity.idle && mcp.hasTool('device.info')) mcp.callTool('device.info', {}).catch(() => {})
      }, () => {})
      return { mcp: serverOf(mcp, command), board: { state: 'no-answer' } }
    }
    const message = String(error?.message ?? error)
    if (/^could not run /.test(message)) {
      return { mcp: { found: false, command, detail: detail(message) }, board: { state: 'unknown' } }
    }
    return { mcp: { ...serverOf(mcp, command), detail: detail(message) }, board: { state: 'unknown' } }
  }
  const server = serverOf(mcp, command)
  // Again, after the await: a tool call may have started while the server
  // did. Sent now, the check could land between its capture and its click.
  if (!activity.idle) return { ...busy, mcp: server }
  if (!mcp.hasTool('device.status')) {
    // clawtouch-mcp before 0.6.1. device.info does no I/O but is a tool
    // call, which is what starts the server's idle release of the port: a
    // server started only for this check would otherwise hold it for good.
    let info
    try {
      if (mcp.hasTool('device.info')) info = (await within(mcp.callTool('device.info', {}), ms)).json?.info
    } catch { /* the version is what matters here */ }
    return { mcp: server, board: { state: 'old-server', ...(info?.port ? { port: String(info.port) } : {}) } }
  }
  let result
  try {
    result = await within(mcp.callTool('device.status', {}), ms)
  } catch (error) {
    if (error?.timeout) return { mcp: server, board: { state: 'no-answer' } }
    return { mcp: server, board: { state: 'unknown', detail: detail(error?.message ?? error) } }
  }
  if (result.isError) return { mcp: server, board: { state: 'unknown', detail: detail(result.text) } }
  const s = result.json ?? {}
  if (s.mode === 'mock') return { mcp: server, board: { state: 'mock' } }
  if (!s.connected) {
    return { mcp: server, board: { state: 'absent', detail: detail(s.detail), ports: s.tried_ports ?? [] } }
  }
  if (!s.responding) {
    return { mcp: server, board: { state: 'silent', port: s.port, detail: detail(s.detail) } }
  }
  return {
    mcp: server,
    board: {
      state: 'connected',
      port: s.port,
      firmware: s.firmware?.version ?? null,
      board: s.firmware?.board ?? null,
      abs: s.abs_pointer ? {
        enabled: Boolean(s.abs_pointer.enabled),
        reason: detail(s.abs_pointer.reason) ?? null,
      } : null,
    },
  }
}

function serverOf(mcp, command) {
  const version = mcp.serverInfo?.version
  return { found: Boolean(mcp.running || version), command, ...(version ? { version: String(version) } : {}) }
}

/**
 * @param {object} deps
 * @param {() => object | undefined} deps.editor
 * @param {object} deps.fiber
 * @param {object} deps.config   the raw config apply() was given
 * @param {() => object | undefined} deps.llm
 * @param {object} deps.mcp      the plugin's McpStdioClient
 * @param {string} deps.command  the executable it runs
 * @param {import('./activity.js').ActivityGate} deps.activity
 */
export function statusRoute({ editor, fiber, config, llm, mcp, command, activity }) {
  // Two pages open, or a double click: one check, both answered with it.
  let inflight = null
  const check = async () => {
    const entry = ownEntry(editor(), fiber)
    const raw = entry?.options?.config ?? config
    const [eye, device] = await Promise.all([checkEye(raw, llm()), checkDevice(mcp, command, activity)])
    return { eye, ...device }
  }
  return {
    path: STATUS_ROUTE,
    methods: ['POST'],
    requestBody: 'buffered',
    fetch: async () => {
      inflight ??= check().finally(() => { inflight = null })
      return json(200, await inflight)
    },
  }
}

/** The routes dsh offers that declare image input, provider by provider. */
export async function listImageRoutes(llm, ms = MODELS_PER_PROVIDER_MS) {
  if (typeof llm?.listProviders !== 'function' || typeof llm?.listModels !== 'function') {
    return { routes: [], failed: [], available: false }
  }
  let providers
  try {
    providers = llm.listProviders()
  } catch {
    return { routes: [], failed: [], available: false }
  }
  // listModels takes no signal: a slow catalog is left to finish on its own,
  // and every provider is asked at once, so the whole list waits `ms` at most.
  const settled = await Promise.allSettled(providers.map((p) =>
    within(Promise.resolve().then(() => llm.listModels(p.id)), ms)))
  const routes = []
  const failed = []
  settled.forEach((s, i) => {
    const provider = providers[i]
    if (s.status === 'rejected' || !Array.isArray(s.value)) {
      failed.push(String(provider.id))
      return
    }
    for (const r of imageRoutes(s.value)) {
      routes.push({ ...r, providerName: provider.name || provider.id })
    }
  })
  return { routes, failed, available: true }
}

export function modelsRoute({ llm, freshMs = MODELS_FRESH_MS }) {
  let inflight = null
  let last = null // { at, body }
  return {
    path: MODELS_ROUTE,
    methods: ['GET'],
    // Required even with no body: without it dsh hands the route a streamed
    // body, which a GET cannot have, and answers 400 (seen on dsh 0.2.2-rc.1).
    requestBody: 'buffered',
    fetch: async () => {
      if (last && Date.now() - last.at < freshMs) return json(200, last.body)
      inflight ??= listImageRoutes(llm())
        .then((body) => { last = { at: Date.now(), body }; return body })
        .finally(() => { inflight = null })
      return json(200, await inflight)
    },
  }
}
