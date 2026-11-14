/**
 * node --test client.test.js — the browser half (client.js).
 *
 * A component that throws blanks its slot entry in dsh with nothing but a
 * console line, so the page is rendered here against a small stand-in for
 * React (state, refs, callbacks and effects, enough for this page) and driven
 * the way a person would: open it, read the status, change a setting, save.
 * The registration it makes is checked against what dsh's Plugins page looks
 * for. What it looks like is checked in a real dsh, not here.
 *
 * The React stand-in is the one dsh-engramory's client tests use.
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const SOURCE = readFileSync(join(HERE, 'client.js'), 'utf8')
const PKG = JSON.parse(readFileSync(join(HERE, 'package.json'), 'utf8'))

/** A synchronous React stand-in: hooks keyed by component path, re-render on demand. */
function miniReact() {
  const stores = new Map()
  let current
  let effects = []
  let dirty = false
  const hook = (init) => {
    const slot = current.index++
    if (!(slot in current.hooks)) current.hooks[slot] = init()
    return [current.hooks, slot]
  }
  const same = (a, b) => a !== undefined && b !== undefined && a.length === b.length && a.every((v, i) => Object.is(v, b[i]))
  const React = {
    createElement: (type, props, ...children) => ({ type, props: { ...(props ?? {}), children: children.flat() } }),
    useState(initial) {
      const [hooks, slot] = hook(() => ({ value: typeof initial === 'function' ? initial() : initial }))
      const cell = hooks[slot]
      cell.set ??= (v) => { cell.value = typeof v === 'function' ? v(cell.value) : v; dirty = true }
      return [cell.value, cell.set]
    },
    useRef(initial) {
      const [hooks, slot] = hook(() => ({ current: initial }))
      return hooks[slot]
    },
    useCallback(fn, deps) {
      const [hooks, slot] = hook(() => ({}))
      const cell = hooks[slot]
      if (!same(cell.deps, deps)) { cell.fn = fn; cell.deps = deps }
      return cell.fn
    },
    useEffect(fn, deps) {
      const [hooks, slot] = hook(() => ({}))
      const cell = hooks[slot]
      if (!same(cell.deps, deps)) { cell.deps = deps; effects.push(fn) }
    },
  }
  const render = (node, path) => {
    if (node === null || node === undefined || node === false || node === true) return null
    if (typeof node === 'string' || typeof node === 'number') return String(node)
    if (Array.isArray(node)) return node.map((child, i) => render(child, `${path}/${child?.props?.key ?? i}`))
    if (typeof node.type === 'function') {
      const id = `${path}:${node.type.name}`
      const store = stores.get(id) ?? { hooks: [] }
      stores.set(id, store)
      const outer = current
      current = { hooks: store.hooks, index: 0 }
      const out = node.type(node.props)
      current = outer
      return render(out, id)
    }
    const children = node.props.children.map((child, i) => render(child, `${path}/${node.type}${child?.props?.key ?? i}`))
    return { type: node.type, props: node.props, children }
  }
  return {
    React,
    mount(element) {
      let tree
      const draw = () => {
        dirty = false
        tree = render(element, 'root')
        const run = effects
        effects = []
        for (const fn of run) fn()
      }
      draw()
      return {
        get tree() { return tree },
        /** Let pending fetches resolve and re-render until the page is still. */
        async settle() {
          for (let i = 0; i < 60; i++) {
            await new Promise((resolve) => setImmediate(resolve))
            if (dirty || effects.length) draw()
          }
        },
      }
    },
  }
}

const textOf = (node) => (node === null ? '' : typeof node === 'string' ? node
  : Array.isArray(node) ? node.map(textOf).join('') : textOf(node.children))
const findAll = (node, pred, out = []) => {
  if (Array.isArray(node)) for (const child of node) findAll(child, pred, out)
  else if (node && typeof node === 'object') {
    if (pred(node)) out.push(node)
    findAll(node.children, pred, out)
  }
  return out
}
const button = (tree, label) => findAll(tree, (n) => n.type === 'button' && textOf(n) === label)[0]
const switchFor = (tree, label) => findAll(tree, (n) => n.props.role === 'switch' && n.props['aria-label'] === label)[0]

/** Load client.js as dsh would, apply it, and return what it registered. */
async function applied() {
  let registered
  globalThis.window = { __ModuleLoader__: { load: (spec) => { registered = spec } } }
  await import(`${pathToFileURL(join(HERE, 'client.js')).href}?${Math.random()}`)
  const react = miniReact()
  const mod = registered.factory((name) => {
    if (name === 'react') return react.React
    throw new Error(`unexpected require ${name}`)
  })
  const dictionaries = {}
  const slots = []
  const styles = []
  globalThis.document = {
    createElement: () => { const tag = { dataset: {}, remove: () => styles.splice(styles.indexOf(tag), 1) }; return tag },
    head: { appendChild: (tag) => styles.push(tag) },
    documentElement: { lang: 'zh-CN' },
  }
  const ctx = {
    effect: (fn) => fn(),
    locale: { register: (ns, dicts) => { dictionaries[ns] = dicts; return () => {} } },
    slots: {
      inject: (owner, fn) => { const before = slots.length; fn(); assert.equal(slots[before].options.name, owner) },
      register: (options, component) => { slots.push({ options, component }); return () => {} },
    },
  }
  mod.apply(ctx)
  return { registered, mod, react, dictionaries, slots, styles }
}

const settingsData = (patch = {}) => ({
  eye: { mode: 'conversation' }, dryRun: false, maxWidth: 1600, command: 'clawtouch-mcp',
  legacy: [], editable: true, revision: 'r1', ...patch,
})
const STATUS = {
  eye: { mode: 'conversation', legacy: [] },
  mcp: { found: true, command: 'clawtouch-mcp', version: '0.6.1' },
  board: { state: 'connected', port: 'COM22', firmware: '1.2.0', board: 'pico2', abs: { enabled: true, reason: null } },
}
const MODELS = {
  routes: [
    { provider: 'bailian', model: 'qwen3.8-max', name: 'qwen3.8-max', providerName: 'Bailian' },
    { provider: 'other', model: 'some-vl', name: 'Some VL', providerName: 'Other' },
  ],
  failed: [],
  available: true,
}

/** A fetch that answers the page's three routes, recording what was sent. */
function fakeFetch(routes) {
  const sent = []
  globalThis.fetch = async (url, init = {}) => {
    const method = init.method ?? 'GET'
    sent.push({ url, method, body: init.body && JSON.parse(init.body) })
    const next = routes[`${method} ${url}`]
    if (next === undefined) throw new Error(`unexpected ${method} ${url}`)
    const value = typeof next === 'function' ? next(sent) : next
    const status = value?.status ?? 200
    return { ok: status < 400, status, json: async () => value?.body ?? value }
  }
  return sent
}

/** setTimeout without the wait, for the save's read-back loop. */
async function fastTimers(fn) {
  const real = globalThis.setTimeout
  globalThis.setTimeout = (cb) => real(cb, 0)
  try { return await fn() } finally { globalThis.setTimeout = real }
}

/** Settle until `pred(text)` holds: a save's read-back takes a variable number of turns. */
async function settleUntil(page, pred, rounds = 200) {
  for (let i = 0; i < rounds && !pred(textOf(page.tree)); i++) await page.settle()
}

const t = (key, params) => (params ? `${key}${JSON.stringify(params)}` : key)

async function openPage(routes) {
  const { slots, react } = await applied()
  const sent = fakeFetch(routes)
  const page = react.mount(react.React.createElement(slots[0].component, { view: 'page', t }))
  await page.settle()
  return { page, sent }
}

test('client: registers the package page only, keyed by package name, with matching dictionaries', async () => {
  const { registered, mod, dictionaries, slots, styles } = await applied()
  assert.equal(registered.id, 'dsh-clawtouch')
  assert.deepEqual(mod.inject, ['slots', 'locale'])
  // the settings live on the package's page; nothing in the sidebar
  assert.deepEqual(slots.map((s) => s.options.name), ['plugins.bundle.config'])
  assert.equal(slots[0].options.key, PKG.name)
  assert.equal(slots[0].options.locale, 'clawtouch')
  assert.equal(styles.length, 1)
  const { zh, en } = dictionaries.clawtouch
  assert.deepEqual(Object.keys(zh).sort(), Object.keys(en).sort())
  const used = new Set([...SOURCE.matchAll(/\bt\('([A-Za-z]+)'[,)]/g)].map((m) => m[1]))
  for (const key of ['eyeProblemTimeout', 'eyeProblemFailed', 'eyeProblemNoLlm', 'imageYes', 'imageNo',
    'imageUnknown', 'stale', 'expression', 'busy']) used.add(key)
  for (const key of used) assert.ok(key in zh && key in en, key)
  const holes = (text) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join()
  for (const key of Object.keys(zh)) assert.equal(holes(zh[key]), holes(en[key]), key)
})

test('client: its version is the package\'s', () => {
  assert.match(SOURCE, new RegExp(`const VERSION = '${PKG.version.replace(/\./g, '\\.')}'`))
})

test('client: the summary view is one line', async () => {
  const { slots } = await applied()
  assert.equal(slots[0].component({ view: 'summary', t: (k) => `<${k}>` }), '<summary>')
})

test('client: on open it reads the settings and models, and checks the status by POST, all relative to the page', async () => {
  const { page, sent } = await openPage({
    'GET api/clawtouch/settings': settingsData(),
    'GET api/clawtouch/models': MODELS,
    'POST api/clawtouch/status': STATUS,
  })
  assert.deepEqual(sent.map((s) => `${s.method} ${s.url}`).sort(),
    ['GET api/clawtouch/models', 'GET api/clawtouch/settings', 'POST api/clawtouch/status'])
  const text = textOf(page.tree)
  assert.match(text, /mcpFound\{"version":"0\.6\.1"\}/)
  assert.match(text, /boardConnected\{"port":"COM22","firmware":"1\.2\.0"\}/)
  assert.match(text, /absOn/)
  // measured models carry their number; others do not
  const options = findAll(page.tree, (n) => n.type === 'option').map(textOf)
  assert.ok(options.some((o) => o.startsWith('eyeOptionMeasured') && o.includes('"pct":96')))
  assert.ok(options.some((o) => o.startsWith('eyeOptionPlain') && o.includes('Some VL')))
  assert.match(text, /eyeMeasuredNote/)
})

test('client: picking a model and turning dry run on saves the whole form, then reads it back', async () => {
  let saved
  await fastTimers(async () => {
    const { page, sent } = await openPage({
      'GET api/clawtouch/settings': (log) => (log.some((s) => s.method === 'POST' && s.url.endsWith('settings'))
        ? settingsData({ eye: { mode: 'route', provider: 'bailian', model: 'qwen3.8-max' }, dryRun: true, revision: 'r2' })
        : settingsData()),
      'POST api/clawtouch/settings': { ok: true },
      'GET api/clawtouch/models': MODELS,
      'POST api/clawtouch/status': STATUS,
    })
    const select = findAll(page.tree, (n) => n.type === 'select')[0]
    select.props.onChange({ target: { value: JSON.stringify(['bailian', 'qwen3.8-max']) } })
    await page.settle()
    switchFor(page.tree, 'dryRunTitle').props.onClick()
    await page.settle()
    assert.match(textOf(page.tree), /saveNote/)
    button(page.tree, 'save').props.onClick()
    await settleUntil(page, (text) => /saved(?!Unconfirmed)/.test(text) && sent.filter((s) => s.url.endsWith('status')).length === 2)
    saved = sent.find((s) => s.method === 'POST' && s.url.endsWith('settings'))
    assert.match(textOf(page.tree), /saved(?!Unconfirmed)/)
    // the status is checked again after the reload
    assert.equal(sent.filter((s) => s.url.endsWith('status')).length, 2)
  })
  assert.deepEqual(saved.body, {
    revision: 'r1',
    eye: { mode: 'route', provider: 'bailian', model: 'qwen3.8-max' },
    dryRun: true, maxWidth: 1600, command: 'clawtouch-mcp', dropLegacy: false,
  })
})

test('client: an invalid width disables Save; a busy refusal is explained', async () => {
  const { page } = await openPage({
    'GET api/clawtouch/settings': settingsData(),
    'POST api/clawtouch/settings': { status: 409, body: { ok: false, error: 'busy' } },
    'GET api/clawtouch/models': MODELS,
    'POST api/clawtouch/status': STATUS,
  })
  const width = findAll(page.tree, (n) => n.type === 'input' && n.props.id === 'ct-width')[0]
  width.props.onChange({ target: { value: '99' } })
  await page.settle()
  assert.equal(button(page.tree, 'save').props.disabled, true)
  width.props.onChange({ target: { value: '1280' } })
  await page.settle()
  assert.equal(button(page.tree, 'save').props.disabled, false)
  button(page.tree, 'save').props.onClick()
  await page.settle()
  assert.match(textOf(page.tree), /busy/)
})

test('client: the 0.1.x settings are named, and removing them sends the saved form, not the draft', async () => {
  await fastTimers(async () => {
    const { page, sent } = await openPage({
      'GET api/clawtouch/settings': (log) => (log.some((s) => s.method === 'POST' && s.url.endsWith('settings'))
        ? settingsData() : settingsData({ legacy: ['endpoint', 'apiKey'] })),
      'POST api/clawtouch/settings': { ok: true },
      'GET api/clawtouch/models': MODELS,
      'POST api/clawtouch/status': { ...STATUS, eye: { mode: 'conversation', legacy: ['endpoint', 'apiKey'] } },
    })
    const text = textOf(page.tree)
    assert.match(text, /legacyBody\{"keys":"vision\.endpoint, vision\.apiKey"\}/)
    assert.match(text, /legacyStatus/)
    switchFor(page.tree, 'dryRunTitle').props.onClick() // an unsaved change
    await page.settle()
    button(page.tree, 'legacyDrop').props.onClick()
    await settleUntil(page, (text) => /saved(?!Unconfirmed)/.test(text))
    const body = sent.find((s) => s.method === 'POST' && s.url.endsWith('settings')).body
    assert.equal(body.dropLegacy, true)
    assert.equal(body.dryRun, false)
    // the unsaved change survives the removal
    assert.equal(switchFor(page.tree, 'dryRunTitle').props['aria-checked'], true)
    assert.equal(button(page.tree, 'save').props.disabled, false)
  })
})

test('client: a 0.1.x config with vision.model alone can still be cleaned up', async () => {
  await fastTimers(async () => {
    const { page, sent } = await openPage({
      'GET api/clawtouch/settings': (log) => (log.some((s) => s.method === 'POST' && s.url.endsWith('settings'))
        ? settingsData()
        : settingsData({ eye: { mode: 'incomplete', provider: '', model: 'qwen-vl-max' }, legacy: ['endpoint', 'apiKey'] })),
      'POST api/clawtouch/settings': { ok: true },
      'GET api/clawtouch/models': MODELS,
      'POST api/clawtouch/status': STATUS,
    })
    assert.match(textOf(page.tree), /legacyHalfEye/)
    button(page.tree, 'legacyDrop').props.onClick()
    await settleUntil(page, (text) => /saved(?!Unconfirmed)/.test(text))
    const body = sent.find((s) => s.method === 'POST' && s.url.endsWith('settings')).body
    assert.deepEqual([body.dropLegacy, body.eye], [true, { mode: 'conversation' }])
    assert.match(textOf(page.tree), /saved(?!Unconfirmed)/)
  })
})

test('client: a save while the first check is still out checks again, and drops the stale answer', async () => {
  await fastTimers(async () => {
    let first
    let n = 0
    const { page, sent } = await openPage({
      'GET api/clawtouch/settings': (log) => (log.some((s) => s.method === 'POST' && s.url.endsWith('settings'))
        ? settingsData({ dryRun: true }) : settingsData()),
      'POST api/clawtouch/settings': { ok: true },
      'GET api/clawtouch/models': MODELS,
      'POST api/clawtouch/status': () => {
        n += 1
        if (n === 1) return new Promise((r) => { first = () => r({ ...STATUS, mcp: { ...STATUS.mcp, version: 'OLD' } }) })
        return STATUS
      },
    })
    switchFor(page.tree, 'dryRunTitle').props.onClick()
    await page.settle()
    button(page.tree, 'save').props.onClick()
    await settleUntil(page, (text) => /saved(?!Unconfirmed)/.test(text))
    first()
    await settleUntil(page, (text) => sent.filter((s) => s.url.endsWith('status')).length === 2 && !/checking/.test(text))
    assert.equal(sent.filter((s) => s.url.endsWith('status')).length, 2)
    assert.doesNotMatch(textOf(page.tree), /OLD/)
    assert.match(textOf(page.tree), /mcpFound\{"version":"0\.6\.1"\}/)
  })
})

test('client: a page that cannot save says why and offers no Save', async () => {
  const { page } = await openPage({
    'GET api/clawtouch/settings': settingsData({ editable: false, readOnly: 'expression' }),
    'GET api/clawtouch/models': MODELS,
    'POST api/clawtouch/status': STATUS,
  })
  assert.match(textOf(page.tree), /expression/)
  assert.equal(button(page.tree, 'save'), undefined)
})

test('client: a configured route missing from the list stays selected rather than being swapped out', async () => {
  const { page } = await openPage({
    'GET api/clawtouch/settings': settingsData({ eye: { mode: 'route', provider: 'gone', model: 'm1' } }),
    'GET api/clawtouch/models': MODELS,
    'POST api/clawtouch/status': STATUS,
  })
  const select = findAll(page.tree, (n) => n.type === 'select')[0]
  assert.equal(select.props.value, JSON.stringify(['gone', 'm1']))
  assert.ok(findAll(select, (n) => n.type === 'option').some((o) => textOf(o).startsWith('eyeOptionUnlisted')))
})

test('client: each board state reads as a sentence', async () => {
  for (const [board, expect] of [
    [{ state: 'busy' }, /boardBusy/],
    [{ state: 'old-server', port: 'COM22' }, /boardOldServer.*COM22/],
    [{ state: 'absent', detail: 'No ClawTouch HID board was found' }, /boardAbsent.*No ClawTouch/],
    [{ state: 'mock' }, /boardMock/],
    [{ state: 'connected', port: 'COM1', firmware: '1.1.0', abs: { enabled: false, reason: 'firmware too old' } },
      /absOff\{"reason":"firmware too old"\}/],
  ]) {
    const { page } = await openPage({
      'GET api/clawtouch/settings': settingsData(),
      'GET api/clawtouch/models': MODELS,
      'POST api/clawtouch/status': { ...STATUS, board },
    })
    assert.match(textOf(page.tree), expect)
  }
  const { page } = await openPage({
    'GET api/clawtouch/settings': settingsData(),
    'GET api/clawtouch/models': MODELS,
    'POST api/clawtouch/status': { ...STATUS, mcp: { found: false, command: 'clawtouch-mcp', detail: 'spawn ENOENT' } },
  })
  assert.match(textOf(page.tree), /mcpMissing.*spawn ENOENT/)
})
