/**
 * Mount smoke test: load the plugin the way the host does, against the
 * host's REAL schema compiler.
 *
 * The failure mode this exists for is the one that does not throw at
 * author time: a tool schema the host rejects, a render() that blows up
 * on its own declared value, a guard that denies the wrong thing. All
 * three install cleanly and only surface on a live host, mid-task.
 *
 * `defineTool`, `assertSupportedJsonSchema` and `validateJsonSchemaValue`
 * come from the installed `@deepseek-ai/dsh-tools`, so what passes here is
 * what the host's own compiler accepts — not what a hand-written mock
 * happens to allow. The context is still a stand-in: this proves the
 * shapes, not that Cordis mounts the plugin. Boot a real profile for that.
 *
 * Run: node smoke.js     (needs @deepseek-ai/dsh-tools resolvable)
 */
import assert from 'node:assert/strict'
import {
  assertSupportedJsonSchema, validateJsonSchemaValue,
} from '@deepseek-ai/dsh-tools'

import { apply, name, inject } from './index.js'

let failed = 0
const check = (label, fn) => {
  try { fn(); console.log(`ok    ${label}`) } catch (err) {
    failed += 1
    console.error(`FAIL  ${label}\n      ${err.message}`)
  }
}

// ── a stand-in context that enforces what the runtime enforces ──

const registered = new Map()
const guards = []
const skills = []
const disposers = []

const ctx = {
  logger: () => ({ info() {}, warn() {}, error() {}, debug() {} }),
  on(event, fn) { if (event === 'dispose') disposers.push(fn) },
  tools: {
    register(definition) {
      // Mirrors ToolRuntime.register()'s own preconditions.
      assert.equal(typeof definition.name, 'string')
      assert.equal(typeof definition.description, 'string')
      assert.ok(definition.output && typeof definition.output.render === 'function',
        `tool "${definition.name}" must declare output { schema, render }`)
      assertSupportedJsonSchema(definition.output.schema)
      assertSupportedJsonSchema(definition.parameters)
      assert.equal(typeof definition.execute, 'function')
      registered.set(definition.name, definition)
      return () => registered.delete(definition.name)
    },
    guard(fn) {
      assert.equal(typeof fn, 'function')
      guards.push(fn)
      return () => {}
    },
  },
  inject(names, cb) {
    // The reactive form: the host calls back once the named services
    // exist. Simulate a profile that DOES have a skill registry.
    const inner = {
      effect: (fn) => fn(),
      skills: { register: (s) => { skills.push(s); return () => {} } },
    }
    cb(inner)
  },
}

apply(ctx, { command: 'clawtouch-mcp', mock: true, dryRun: true })

// ── shape ──

check('exports the metadata Cordis reads', () => {
  assert.equal(name, 'clawtouch')
  assert.deepEqual(inject, ['tools'])
})

check('registers the seven agent-facing tools', () => {
  assert.deepEqual([...registered.keys()].sort(), [
    'computer_click', 'computer_click_sequence', 'computer_find',
    'computer_key', 'computer_scroll', 'computer_type', 'computer_windows',
  ])
})

check('registers one guard and one skill', () => {
  assert.equal(guards.length, 1)
  assert.equal(skills.length, 1)
  assert.equal(skills[0].name, 'clawtouch-computer-use')
  assert.ok(skills[0].content.includes('computer_click'))
  assert.equal(skills[0].source, 'runtime')
})

// ── every tool's declared schemas actually accept its own data ──

const SAMPLES = {
  computer_click: {
    args: { target: 'the Send button', window: 'WeChat' },
    value: { clicked: true, summary: 'clicked (100, 200)', x: 100, y: 200 },
  },
  computer_click_sequence: {
    args: { targets: ['the 5 key', 'the plus key'], window: 'Calculator' },
    value: { clicked: true, count: 2, summary: 'clicked 2 in Calculator' },
  },
  computer_find: {
    args: { target: 'the search box' },
    value: { found: true, summary: 'found (10, 20)', x: 10, y: 20 },
  },
  computer_windows: {
    args: {},
    // Both platform shapes in one value, because the schema has to admit
    // both and only the real validator can say so: Windows measures every
    // field, macOS measures none of the guards, and `foreground` can also
    // go missing on either when the frontmost query itself fails. A
    // required `foreground` would have rejected the third window here.
    value: { windows: [
      { title: 'A', foreground: true, width: 800, height: 600,
        visible_percent: 100, accepts_input: true },
      { title: 'B', foreground: false, width: 400, height: 300 },
      { title: 'C', width: 200, height: 100 },
    ] },
  },
  computer_type: {
    args: { text: 'hello', target: 'the message box' },
    value: { summary: 'typed 5 characters' },
  },
  computer_key: {
    args: { key: 'enter', modifiers: ['ctrl'] },
    value: { summary: 'pressed ctrl+enter' },
  },
  computer_scroll: {
    args: { amount: -3 },
    value: { summary: 'scrolled -3' },
  },
}

for (const [toolName, sample] of Object.entries(SAMPLES)) {
  check(`${toolName}: arguments validate`, () => {
    const tool = registered.get(toolName)
    const violations = validateJsonSchemaValue(tool.parameters, sample.args, '')
    assert.equal(violations.length, 0, JSON.stringify(violations))
  })
  check(`${toolName}: output value validates and renders`, () => {
    const tool = registered.get(toolName)
    const violations = validateJsonSchemaValue(tool.output.schema, sample.value, 'value')
    assert.equal(violations.length, 0, JSON.stringify(violations))
    const blocks = tool.output.render(sample.args, sample.value)
    assert.ok(Array.isArray(blocks) && blocks.length >= 1)
    assert.equal(blocks[0].type, 'text')
    assert.ok(typeof blocks[0].text === 'string' && blocks[0].text.length > 0)
  })
}

check('a missing required argument is rejected', () => {
  const tool = registered.get('computer_click')
  const violations = validateJsonSchemaValue(tool.parameters, {}, '')
  assert.ok(violations.length > 0, 'expected `target` to be required')
})

// ── what the model is told ──

check('model-facing text keeps to what the plugin does', () => {
  // The agent needs to know the input is real and has no undo — and nothing
  // this plugin does not itself measure. Covers the skill and every tool.
  const texts = [
    skills[0].content, skills[0].description, skills[0].whenToUse,
    ...[...registered.values()].flatMap((t) => [
      t.description, JSON.stringify(t.parameters),
    ]),
  ]
  const unmeasured = /cannot tell|genuine physical/i
  for (const text of texts) assert.doesNotMatch(String(text), unmeasured)
})

let pendingAsync = Promise.resolve()
const checkAsync = (label, fn) => {
  pendingAsync = pendingAsync.then(() => fn().then(
    () => console.log(`ok    ${label}`),
    (err) => { failed += 1; console.error(`FAIL  ${label}\n      ${err.message}`) }))
}

checkAsync('computer_type refuses untypeable text before touching anything', async () => {
  // Refused before the target click too: a field clicked for text that was
  // never going to be typed is still a change on screen. Nothing is spawned
  // here — this has to fail before the plugin reaches for the server.
  const tool = registered.get('computer_type')
  await assert.rejects(
    () => tool.execute({ text: 'Hello，世界', target: 'the message box' }),
    /nothing was typed or clicked: the text contains "，" "世" "界"/)
})

// ── the guard's decision table ──

const guard = guards[0]
const decide = (toolName, args) => guard({ name: toolName, arguments: args })

// Which combos quit or switch windows depends on the OS the guard runs on
// (lib/keyguard.js), and this runs on whatever the tester has: use the
// spellings that platform refuses. test.js covers every platform's table.
const MAC = process.platform === 'darwin'
const QUIT = MAC
  ? { name: 'Cmd+Q', args: { key: 'q', modifiers: ['cmd'] }, shorthand: 'cmd+q' }
  : { name: 'Alt+F4', args: { key: 'F4', modifiers: ['alt'] }, shorthand: 'alt+f4' }
const CLOSE = MAC
  ? { name: 'Cmd+W', args: { key: 'w', modifiers: ['cmd'] } }
  : { name: 'Ctrl+W', args: { key: 'w', modifiers: ['ctrl'] } }
const SWITCH = MAC
  ? { name: 'Cmd+Tab', args: { key: 'tab', modifiers: ['cmd'] }, shorthand: 'Cmd+Tab' }
  : { name: 'Alt+Tab', args: { key: 'tab', modifiers: ['alt'] }, shorthand: 'Alt+Tab' }
const QUIT_MSG = /ends this session/
const SWITCH_MSG = /moves focus/

check(`guard denies ${QUIT.name}`, () => {
  assert.match(String(decide('computer_key', QUIT.args)), QUIT_MSG)
})
check(`guard denies ${CLOSE.name}`, () => {
  assert.match(String(decide('computer_key', CLOSE.args)), QUIT_MSG)
})
check(`guard denies ${QUIT.name} written as shorthand in \`key\``, () => {
  // clawtouch-mcp splits "alt+f4" itself; the guard used to miss it
  assert.match(String(decide('computer_key', { key: QUIT.shorthand })), QUIT_MSG)
})
check(`guard denies ${SWITCH.name}`, () => {
  assert.match(String(decide('computer_key', SWITCH.args)), SWITCH_MSG)
  assert.match(String(decide('computer_key', { key: SWITCH.shorthand })), SWITCH_MSG)
})
check('guard allows an ordinary combo', () => {
  assert.equal(decide('computer_key', { key: 'c', modifiers: ['ctrl'] }), undefined)
  assert.equal(decide('computer_key', { key: 'tab', modifiers: ['shift'] }), undefined)
})
check('guard allows q without a GUI modifier', () => {
  assert.equal(decide('computer_key', { key: 'q', modifiers: [] }), undefined)
})
check('guard never touches other tools', () => {
  assert.equal(decide('write', { file_path: 'x', content: 'y' }), undefined)
  assert.equal(decide('computer_click', { target: 'quit' }), undefined)
})
check('guard survives malformed arguments', () => {
  assert.equal(decide('computer_key', {}), undefined)
  assert.equal(decide('computer_key', { key: 42, modifiers: 'gui' }), undefined)
})

// ── opting out ──

const plain = { ...ctx, tools: { register: () => () => {}, guard: () => () => {} } }
const guardsFor = (config) => {
  const seen = []
  apply({ ...plain, tools: { register: () => () => {}, guard: (f) => { seen.push(f); return () => {} } } },
    { registerSkill: false, ...config })
  return seen
}
check('both opt-outs together register no guard', () => {
  assert.equal(guardsFor({ allowQuitCombos: true, allowFocusSwitchCombos: true }).length, 0)
})
check(`allowQuitCombos alone still refuses ${SWITCH.name}, and lets ${QUIT.name} through`, () => {
  const [g] = guardsFor({ allowQuitCombos: true })
  assert.match(String(g({ name: 'computer_key', arguments: SWITCH.args })), SWITCH_MSG)
  assert.equal(g({ name: 'computer_key', arguments: QUIT.args }), undefined)
})
check(`allowFocusSwitchCombos alone still refuses ${QUIT.name}, and lets ${SWITCH.name} through`, () => {
  const [g] = guardsFor({ allowFocusSwitchCombos: true })
  assert.match(String(g({ name: 'computer_key', arguments: QUIT.args })), QUIT_MSG)
  assert.equal(g({ name: 'computer_key', arguments: SWITCH.args }), undefined)
})

await pendingAsync
console.log(failed === 0 ? '\nsmoke ok' : `\n${failed} smoke failures`)
process.exit(failed === 0 ? 0 : 1)
