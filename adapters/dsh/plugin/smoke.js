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
    value: { windows: [{ title: 'A', foreground: true, width: 800,
      height: 600, visible_percent: 100, accepts_input: true }] },
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

// ── the guard's decision table ──

const guard = guards[0]
const decide = (toolName, args) => guard({ name: toolName, arguments: args })

check('guard denies Cmd+Q', () => {
  assert.match(String(decide('computer_key', { key: 'q', modifiers: ['gui'] })),
    /blocked/)
})
check('guard denies Alt+F4', () => {
  assert.match(String(decide('computer_key', { key: 'F4', modifiers: ['alt'] })),
    /blocked/)
})
check('guard denies Cmd+W', () => {
  assert.match(String(decide('computer_key', { key: 'w', modifiers: ['cmd'] })),
    /blocked/)
})
check('guard allows an ordinary combo', () => {
  assert.equal(decide('computer_key', { key: 'c', modifiers: ['ctrl'] }), undefined)
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
check('allowQuitCombos: true registers no guard', () => {
  const seen = []
  apply({ ...plain, tools: { register: () => () => {}, guard: (f) => { seen.push(f); return () => {} } } },
    { allowQuitCombos: true, registerSkill: false })
  assert.equal(seen.length, 0)
})

console.log(failed === 0 ? '\nsmoke ok' : `\n${failed} smoke failures`)
process.exit(failed === 0 ? 0 : 1)
