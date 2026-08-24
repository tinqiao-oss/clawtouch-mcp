/**
 * Unit tests for the parts that must be right before any hardware moves:
 * the coordinate maths and the model-reply parsing.
 *
 * These are the two places where a bug is silent — a wrong scale still
 * produces a plausible-looking coordinate, and a lenient parser still
 * produces a point. Everything else (spawning the server, the vision
 * call) fails loudly on its own.
 *
 * Run: node test.js
 */
import assert from 'node:assert/strict'

import {
  calibrate, toImagePoint, toScreenPoint, resolvePoint, CalibrationError,
} from './lib/calibrate.js'
import { parseAnswer, VisionError } from './lib/vision.js'
import { assertOnTop, unmeasuredNote, LocateError } from './lib/locator.js'
import { McpStdioClient } from './lib/mcp-client.js'
import { Locator } from './lib/locator.js'

let passed = 0
let failed = 0

function test(name, fn) {
  try {
    fn()
    passed += 1
  } catch (err) {
    failed += 1
    console.error(`FAIL  ${name}\n      ${err.message}`)
  }
}

// Markers as hid.screenshot reports them for a 1600x900 capture.
const MARKERS = [
  { id: 'tl', center: [26.5, 26.5], size: 41 },
  { id: 'br', center: [1573.5, 873.5], size: 41 },
]
const META = {
  width: 1600,
  height: 900,
  capture_rect: [5203, 70, 7395, 1322],
  image_scale: [0.729927, 0.730032],
  markers: MARKERS,
}

/** Simulate a model that rescales by `k` with no offset. */
function reportedAt(point, k, offset = [0, 0]) {
  return [point[0] * k + offset[0], point[1] * k + offset[1]]
}

// ── calibration ──

test('pure rescale recovers the scale exactly', () => {
  const k = 0.7688
  const fit = calibrate(MARKERS, {
    tl: reportedAt(MARKERS[0].center, k),
    br: reportedAt(MARKERS[1].center, k),
  })
  assert.ok(Math.abs(fit.x.scale - k) < 1e-9, `x scale ${fit.x.scale}`)
  assert.ok(Math.abs(fit.y.scale - k) < 1e-9, `y scale ${fit.y.scale}`)
  assert.ok(Math.abs(fit.x.offset) < 1e-6)
})

test('a constant offset is absorbed — the case a one-point ratio gets wrong', () => {
  // Letterboxing/padding shifts every reported coordinate by a constant.
  // A single-point fit would fold that shift into the scale and be wrong
  // everywhere except at the marker itself.
  const k = 0.77
  const offset = [14, -9]
  const fit = calibrate(MARKERS, {
    tl: reportedAt(MARKERS[0].center, k, offset),
    br: reportedAt(MARKERS[1].center, k, offset),
  })
  assert.ok(Math.abs(fit.x.scale - k) < 1e-9)
  assert.ok(Math.abs(fit.x.offset - offset[0]) < 1e-6)

  const truth = [800, 450]
  const back = toImagePoint(fit, reportedAt(truth, k, offset))
  assert.ok(Math.abs(back[0] - truth[0]) < 1e-6, `x ${back[0]}`)
  assert.ok(Math.abs(back[1] - truth[1]) < 1e-6, `y ${back[1]}`)

  // What a naive single-point ratio would have produced, for contrast.
  const naive = reportedAt(truth, k, offset)[0] / (
    reportedAt(MARKERS[0].center, k, offset)[0] / MARKERS[0].center[0])
  assert.ok(Math.abs(naive - truth[0]) > 50,
    `expected the naive ratio to be badly off, got ${naive}`)
})

test('image point maps back to the right screen pixel', () => {
  const screen = toScreenPoint([800, 450], META.capture_rect, META.image_scale)
  // 5203 + 800/0.729927 = 5203 + 1096 = 6299
  assert.equal(screen[0], 6299)
  assert.equal(screen[1], 686)
})

test('a full round trip lands within a pixel of the truth', () => {
  const k = 0.7688
  const truthImage = [1204, 612]
  const truthScreen = toScreenPoint(truthImage, META.capture_rect, META.image_scale)
  const fit = calibrate(MARKERS, {
    tl: reportedAt(MARKERS[0].center, k),
    br: reportedAt(MARKERS[1].center, k),
  })
  const got = resolvePoint(fit, reportedAt(truthImage, k), META)
  assert.ok(Math.abs(got.screen[0] - truthScreen[0]) <= 1,
    `${got.screen[0]} vs ${truthScreen[0]}`)
  assert.ok(Math.abs(got.screen[1] - truthScreen[1]) <= 1)
})

test('a misread marker is refused, not silently clicked', () => {
  const k = 0.77
  assert.throws(() => calibrate(MARKERS, {
    tl: reportedAt(MARKERS[0].center, k),
    // y read from the wrong marker → the axes disagree wildly.
    br: [MARKERS[1].center[0] * k, MARKERS[1].center[1] * k * 0.4],
  }), CalibrationError)
})

test('one marker is not enough', () => {
  assert.throws(
    () => calibrate(MARKERS, { tl: [20, 20] }), CalibrationError)
})

test('a point outside the capture is refused', () => {
  const k = 0.77
  const fit = calibrate(MARKERS, {
    tl: reportedAt(MARKERS[0].center, k),
    br: reportedAt(MARKERS[1].center, k),
  })
  assert.throws(
    () => resolvePoint(fit, reportedAt([4000, 450], k), META), CalibrationError)
})

test('a zero image_scale is refused rather than dividing by zero', () => {
  assert.throws(
    () => toScreenPoint([10, 10], [0, 0, 100, 100], [0, 1]), CalibrationError)
})

// ── reply parsing ──

test('plain JSON parses', () => {
  const out = parseAnswer('{"markers":{"tl":[20,20],"br":[1210,672]},'
    + '"target":{"found":true,"point":[615,349],"label":"Send","confidence":0.9}}')
  assert.deepEqual(out.markers.tl, [20, 20])
  assert.deepEqual(out.target.point, [615, 349])
  assert.equal(out.target.label, 'Send')
  assert.equal(out.target.confidence, 0.9)
})

test('a fenced reply parses — models add fences despite being told not to', () => {
  const out = parseAnswer('```json\n{"markers":{"tl":[1,2],"br":[3,4]},'
    + '"target":{"found":true,"point":[5,6]}}\n```')
  assert.deepEqual(out.target.point, [5, 6])
})

test('a reply with commentary around the JSON parses', () => {
  const out = parseAnswer('Sure! Here are the coordinates:\n'
    + '{"markers":{"tl":[1,2],"br":[3,4]},"target":{"found":true,"point":[7,8]}}\n'
    + 'Hope that helps.')
  assert.deepEqual(out.target.point, [7, 8])
})

test('{x,y} objects are accepted as points', () => {
  const out = parseAnswer('{"markers":{"tl":{"x":1,"y":2},"br":{"x":3,"y":4}},'
    + '"target":{"found":true,"point":{"x":9,"y":10}}}')
  assert.deepEqual(out.markers.tl, [1, 2])
  assert.deepEqual(out.target.point, [9, 10])
})

test('found:false is preserved, not turned into a point', () => {
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},'
    + '"target":{"found":false}}')
  assert.equal(out.target.found, false)
  assert.equal(out.target.point, undefined)
})

test('a point-less target reads as not found even when found says true', () => {
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},'
    + '"target":{"found":true}}')
  assert.equal(out.target.found, false)
})

test('non-JSON prose fails loudly', () => {
  assert.throws(
    () => parseAnswer('I could not find that element on the screen.'),
    VisionError)
})

// ── guards added after an adversarial review (2026-08-23) ──
//
// Every one of these was a path that produced a CONFIDENT WRONG CLICK
// rather than an error — the one class of bug this design cannot
// tolerate, because the agent has no other way to find out.

test('swapped markers are refused, not silently mirrored', () => {
  // The model labelled the bottom-right marker "tl" and vice versa. Both
  // axes then fit with scale -1, the skew between them is zero, and every
  // target comes back mirrored through the centre — a fit that looks
  // perfectly healthy to every isotropy test.
  const k = 0.77
  assert.throws(() => calibrate(MARKERS, {
    tl: reportedAt(MARKERS[1].center, k),
    br: reportedAt(MARKERS[0].center, k),
  }), (err) => err instanceof CalibrationError && /mirrored/.test(err.message))
})

test('markers are read by name, not by position in the object', () => {
  const k = 0.77
  // The same two points emitted in the other key order. Taking "the first
  // two keys" would pair br's coordinates with tl's known centre.
  const fit = calibrate(MARKERS, {
    br: reportedAt(MARKERS[1].center, k),
    tl: reportedAt(MARKERS[0].center, k),
  })
  assert.ok(Math.abs(fit.x.scale - k) < 1e-9)
})

test('per-axis normalisation onto a square is accepted', () => {
  // Several vision models map each axis independently onto 0-1000. For a
  // 1600x900 image that is a legitimate 44% split between the axes, which
  // a flat "the scales must agree" rule rejects — refusing to work with
  // those models at all.
  const kx = 1000 / 1600
  const ky = 1000 / 900
  const fit = calibrate(MARKERS, {
    tl: [MARKERS[0].center[0] * kx, MARKERS[0].center[1] * ky],
    br: [MARKERS[1].center[0] * kx, MARKERS[1].center[1] * ky],
  }, { width: 1600, height: 900 })
  assert.ok(Math.abs(fit.x.scale - kx) < 1e-9, String(fit.x.scale))
  assert.ok(Math.abs(fit.y.scale - ky) < 1e-9, String(fit.y.scale))
})

test('anisotropy matching neither hypothesis is still refused', () => {
  assert.throws(() => calibrate(MARKERS, {
    tl: [MARKERS[0].center[0] * 0.77, MARKERS[0].center[1] * 0.30],
    br: [MARKERS[1].center[0] * 0.77, MARKERS[1].center[1] * 0.30],
  }, { width: 1600, height: 900 }), CalibrationError)
})

test('a non-numeric marker coordinate is refused', () => {
  assert.throws(() => calibrate(MARKERS, {
    tl: [null, null], br: [100, 100],
  }), CalibrationError)
  assert.throws(() => calibrate(MARKERS, {
    tl: ['left', 'top'], br: [100, 100],
  }), CalibrationError)
})

test('a point past the edge is clamped into the capture, not passed through', () => {
  const fit = calibrate(MARKERS, {
    tl: MARKERS[0].center, br: MARKERS[1].center,
  })
  // One pixel past the right edge: inside the rounding tolerance, so
  // accepted — but the resulting point must still be INSIDE what was
  // captured, or it is a click on something nobody looked at.
  const got = resolvePoint(fit, [META.width, 400], META)
  assert.equal(got.image[0], META.width - 1)
  const [left, top, right, bottom] = META.capture_rect
  assert.ok(got.screen[0] >= left && got.screen[0] < right, String(got.screen))
  assert.ok(got.screen[1] >= top && got.screen[1] < bottom, String(got.screen))
})

test('"false" as a string counts as not found', () => {
  // Models emit the string as readily as the boolean, and `"false" !== false`
  // turned an explicit "it is not on screen" into a click.
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},'
    + '"target":{"found":"false","point":[25,25]}}')
  assert.equal(out.target.found, false)
})

test('null coordinates do not become a click at the origin', () => {
  // Number(null) is 0, so a lenient parser answers "found it, at (0,0)".
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},'
    + '"target":{"found":true,"point":[null,null]}}')
  assert.equal(out.target.found, false)
  assert.equal(out.target.point, undefined)
})

test('an empty-string coordinate does not become 0', () => {
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},'
    + '"target":{"found":true,"point":["",""]}}')
  assert.equal(out.target.found, false)
})

test('numeric strings are still accepted', () => {
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},'
    + '"target":{"found":true,"point":["615","349"]}}')
  assert.deepEqual(out.target.point, [615, 349])
})

test('a non-numeric confidence is dropped rather than reported', () => {
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},'
    + '"target":{"found":true,"point":[5,6],"confidence":"high"}}')
  assert.equal(out.target.confidence, undefined)
})

test('a window hidden behind another one is refused', () => {
  // Capturing a window's rectangle captures whatever is IN FRONT of it.
  // Asked about a covered window, a vision model fluently describes the
  // app on top — an answer indistinguishable from a right one. Found on
  // real hardware: a calculator behind an editor produced "there is no
  // 8 key here, this is a file explorer", which was entirely correct and
  // entirely useless.
  assert.throws(
    () => assertOnTop({ title: 'Calculator', visible_fraction: 0 }),
    (err) => err instanceof LocateError && /in front of it/.test(err.message))
})

test('a corner clipped by a toast is not a reason to stop', () => {
  assert.equal(assertOnTop({ title: 'X', visible_fraction: 0.88 }), undefined)
  assert.equal(assertOnTop({ title: 'X', visible_fraction: 1 }), undefined)
})

test('a window with no visibility measurement is allowed through', () => {
  // macOS and older servers do not report it; refusing everything there
  // would trade a rare wrong answer for a total outage.
  assert.equal(assertOnTop({ title: 'X' }), undefined)
})

test('a window that is visible but disabled is refused', () => {
  // A modal dialog elsewhere disables the window. It looks completely
  // normal, screenshots completely normally, and swallows every click —
  // a physical mouse's included. Checked BEFORE occlusion, because a
  // disabled window is usually unobstructed and would sail through that.
  assert.throws(
    () => assertOnTop({ title: 'WeChat', visible_fraction: 1, enabled: false }),
    (err) => err instanceof LocateError && /not accepting input/.test(err.message))
})

test('enabled windows pass, and an absent flag is not treated as disabled', () => {
  assert.equal(assertOnTop({ title: 'X', visible_fraction: 1, enabled: true }), undefined)
  assert.equal(assertOnTop({ title: 'X', visible_fraction: 1 }), undefined)
})

// ── the child must not hold the host process open ──

function fakeChild() {
  const log = []
  const stream = (name) => ({
    ref() { log.push(`ref:${name}`) },
    unref() { log.push(`unref:${name}`) },
  })
  return {
    log,
    stdout: stream('stdout'),
    stderr: stream('stderr'),
    stdin: stream('stdin'),
    ref() { log.push('ref:child') },
    unref() { log.push('unref:child') },
  }
}

test('an idle client releases every handle it holds', () => {
  // A piped child and its three stdio streams each keep Node's event loop
  // alive. Measured before this was fixed: a one-shot `dsh` run produced
  // its answer in 6 seconds and then sat there, finished, until it was
  // killed two minutes later — which reads as "this tool is unusably
  // slow" and is nothing of the kind.
  const c = new McpStdioClient({ command: 'x' })
  c.child = fakeChild()
  c._updateHandleRefs()
  assert.deepEqual(c.child.log.sort(),
    ['unref:child', 'unref:stderr', 'unref:stdin', 'unref:stdout'])
})

test('a client with a call in flight holds them', () => {
  // Otherwise the loop could exit mid-call and the reply would be lost.
  const c = new McpStdioClient({ command: 'x' })
  c.child = fakeChild()
  c._pending.set(1, { resolve() {}, reject() {}, timer: null })
  c._updateHandleRefs()
  assert.deepEqual(c.child.log.sort(),
    ['ref:child', 'ref:stderr', 'ref:stdin', 'ref:stdout'])
})

test('handles are released again once the last call resolves', () => {
  const c = new McpStdioClient({ command: 'x' })
  c.child = fakeChild()
  c._pending.set(7, { resolve() {}, reject() {}, timer: null })
  c._updateHandleRefs()
  c.child.log.length = 0
  // Simulate the reply arriving for id 7.
  c._onStdout('{"jsonrpc":"2.0","id":7,"result":{}}' + String.fromCharCode(10))
  assert.ok(c.child.log.includes('unref:stdout'), c.child.log.join(','))
  assert.equal(c._pending.size, 0)
})

// ── several targets from one look ──

test('several targets come back in the order they were asked', () => {
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},"targets":['
    + '{"n":2,"point":[20,20]},{"n":1,"point":[10,10]},{"n":3,"point":[30,30]}]}', 3)
  assert.deepEqual(out.targets.map((t) => t.point),
    [[10, 10], [20, 20], [30, 30]])
  // The single-target field keeps working for the one-target path.
  assert.deepEqual(out.target.point, [10, 10])
})

test('a spurious extra entry does not lose the good answers', () => {
  // Observed from the real model: asked for four, answered with five,
  // the fifth a duplicate of the fourth. Failing the whole batch over a
  // stray item would throw away four correct locations.
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},"targets":['
    + '{"n":1,"point":[10,10]},{"n":2,"point":[20,20]},'
    + '{"n":3,"point":[30,30]},{"n":9,"point":[90,90]}]}', 3)
  assert.equal(out.targets.length, 3)
  assert.deepEqual(out.targets[2].point, [30, 30])
})

test('a missing index reads as not found, not as a neighbour point', () => {
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},"targets":['
    + '{"n":1,"point":[10,10]},{"n":3,"point":[30,30]}]}', 3)
  assert.equal(out.targets[0].found, true)
  assert.equal(out.targets[1].found, false)
  assert.equal(out.targets[1].point, undefined)
  assert.equal(out.targets[2].found, true)
})

test('a duplicated index keeps the first answer', () => {
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},"targets":['
    + '{"n":1,"point":[10,10]},{"n":1,"point":[99,99]}]}', 1)
  assert.deepEqual(out.targets[0].point, [10, 10])
})

test('a per-target denial is honoured inside a batch', () => {
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},"targets":['
    + '{"n":1,"found":"false","point":[10,10]},{"n":2,"point":[20,20]}]}', 2)
  assert.equal(out.targets[0].found, false)
  assert.equal(out.targets[1].found, true)
})

test('the single-target reply still produces a one-element list', () => {
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},'
    + '"target":{"found":true,"point":[5,6]}}')
  assert.equal(out.targets.length, 1)
  assert.deepEqual(out.targets[0].point, [5, 6])
})

// ── bringing a window forward ──

/** A Locator over a scripted MCP client, recording what it was asked. */
function locatorWith(replies, config = {}) {
  const calls = []
  const mcp = {
    async callTool(name, args) {
      calls.push({ name, args })
      const next = replies[name]
      const value = typeof next === 'function' ? next(calls) : next
      return value ?? { json: {}, text: '', images: [], isError: false }
    },
  }
  return { locator: new Locator({ mcp, config }), calls }
}

const ONTOP = {
  title: 'Calc', rect: [0, 0, 300, 500], foreground: true,
  visible_fraction: 1, enabled: true, raise_point: [40, 8],
}

async function asyncTest(name, fn) {
  try { await fn(); passed += 1 } catch (err) {
    failed += 1
    console.error(`FAIL  ${name}
      ${err.message}`)
  }
}

await asyncTest('a window already in front is not clicked at', async () => {
  const { locator, calls } = locatorWith({})
  const out = await locator.ensureReachable(ONTOP)
  assert.equal(out.title, 'Calc')
  assert.equal(calls.length, 0, 'nothing should have been sent')
})

await asyncTest('a background window is raised by clicking its caption', async () => {
  // Not by a focus-stealing API: the same physical mouse, on a point the
  // application itself calls a drag area (which narrows it down but does
  // not settle it, hence the re-read afterwards).
  const { locator, calls } = locatorWith({
    'device.info': { json: { screen: { width: 1920, height: 1080, source: 'explicit' } } },
    'hid.click': { json: { ok: true, clicked: true } },
    'screen.windows': { json: { window: { ...ONTOP } } },
  })
  const out = await locator.ensureReachable({ ...ONTOP, foreground: false })
  const click = calls.find((c) => c.name === 'hid.click')
  assert.ok(click, 'expected a raise click')
  assert.deepEqual([click.args.x, click.args.y], [40, 8])
  assert.equal(out.foreground, true, 'the result must be the re-read window')
})

await asyncTest('a modal-disabled window is refused, never raised', async () => {
  // Raising it would change nothing: it discards clicks either way.
  const { locator, calls } = locatorWith({})
  await assert.rejects(
    () => locator.ensureReachable({ ...ONTOP, enabled: false }),
    (err) => err instanceof LocateError && /not accepting input/.test(err.message))
  assert.equal(calls.length, 0)
})

await asyncTest('a buried window with nowhere safe to click is refused', async () => {
  const { locator } = locatorWith({})
  await assert.rejects(
    () => locator.ensureReachable({
      ...ONTOP, foreground: false, visible_fraction: 0, raise_point: undefined,
    }),
    (err) => err instanceof LocateError && /in front of it/.test(err.message))
})

await asyncTest('autoRaise:false leaves the screen alone', async () => {
  const { locator, calls } = locatorWith({}, { autoRaise: false })
  // Visible, merely unfocused: carry on rather than refuse over it.
  const out = await locator.ensureReachable({ ...ONTOP, foreground: false })
  assert.equal(out.title, 'Calc')
  assert.equal(calls.length, 0)
})

await asyncTest('an unconfirmed raise click fails loudly', async () => {
  // Reporting a raise that did not happen would send the next screenshot
  // at whatever is still in front.
  const { locator } = locatorWith({
    'device.info': { json: { screen: { width: 1920, height: 1080, source: 'explicit' } } },
    'hid.click': { json: { ok: false, hint: 'no convergence' } },
  })
  await assert.rejects(
    () => locator.ensureReachable({ ...ONTOP, foreground: false }),
    (err) => err instanceof LocateError && /not confirmed/.test(err.message))
})

await asyncTest('a raise that did not take is caught by re-reading', async () => {
  // The click may raise something else entirely; the state afterwards is
  // read back rather than assumed.
  const { locator } = locatorWith({
    'device.info': { json: { screen: { width: 1920, height: 1080, source: 'explicit' } } },
    'hid.click': { json: { ok: true, clicked: true } },
    'screen.windows': { json: { window: { ...ONTOP, foreground: false, visible_fraction: 0 } } },
  })
  await assert.rejects(
    () => locator.ensureReachable({ ...ONTOP, foreground: false, visible_fraction: 0 }),
    (err) => err instanceof LocateError && /in front of it/.test(err.message))
})

await asyncTest('a re-read that errors is not a raise that worked',
  async () => {
    // Falling back to the pre-click window here would be exactly the
    // assumption the re-read exists to replace, and the next screenshot
    // would be taken at a rectangle nobody confirmed.
    const { locator } = locatorWith({
      'device.info': { json: { screen: { width: 1920, height: 1080, source: 'explicit' } } },
      'hid.click': { json: { ok: true, clicked: true } },
      'screen.windows': { json: { hint: 'enumeration failed' }, text: '', isError: true },
    })
    await assert.rejects(
      () => locator.ensureReachable({ ...ONTOP, foreground: false }),
      (err) => err instanceof LocateError
        && /could not be read back/.test(err.message))
  })

await asyncTest('a re-read that returns no window is refused too', async () => {
  // The window may have been closed, or renamed, while we reached for it.
  const { locator } = locatorWith({
    'device.info': { json: { screen: { width: 1920, height: 1080, source: 'explicit' } } },
    'hid.click': { json: { ok: true, clicked: true } },
    'screen.windows': {
      json: { available: ['Something else'] }, text: '', isError: false,
    },
  })
  await assert.rejects(
    () => locator.ensureReachable({ ...ONTOP, foreground: false }),
    (err) => err instanceof LocateError
      && /could not be read back/.test(err.message))
})

test('an unmeasured window says exactly which guard did not run', () => {
  // assertOnTop lets it through — refusing would make the plugin
  // unusable wherever these cannot be measured — so the answer itself
  // has to carry which guard never ran. Naming only one of them would
  // leave the other silently unmeasured.
  assert.equal(unmeasuredNote({ visible_fraction: 1, enabled: true }), '')
  assert.equal(unmeasuredNote({ visible_fraction: 0.4, enabled: false }), '')
  // macOS measures neither.
  assert.match(unmeasuredNote({ title: 'Safari' }),
    /input state and occlusion unmeasured/)
  // Minimised on Windows, or a rectangle too small to sample: the input
  // state is known, the occlusion figure is not.
  assert.match(unmeasuredNote({ enabled: true }), /occlusion unmeasured/)
  assert.doesNotMatch(unmeasuredNote({ enabled: true }), /input state/)
  // ...and the reverse, so neither branch can quietly disappear.
  assert.match(unmeasuredNote({ visible_fraction: 1 }),
    /input state unmeasured/)
  assert.doesNotMatch(unmeasuredNote({ visible_fraction: 1 }), /occlusion/)
})

await asyncTest('a raise point outside the declared screen says why', async () => {
  // Not raising looks identical to a window that needed no raise, and the
  // cause (a second monitor this session was never told about) cannot be
  // guessed from the outcome. It must also not click: the point would be
  // clamped to somewhere else entirely.
  const logs = []
  const calls = []
  const mcp = {
    async callTool(name, args) {
      calls.push({ name, args })
      if (name === 'device.info') {
        return {
          json: { screen: { width: 1920, height: 1080, source: 'explicit' } },
          text: '', images: [], isError: false,
        }
      }
      return { json: {}, text: '', images: [], isError: false }
    },
  }
  const locator = new Locator({
    mcp, config: {}, log: (level, m) => logs.push(`${level}:${m}`),
  })
  const out = await locator.ensureReachable({
    ...ONTOP, foreground: false, raise_point: [7460, 8],
  })
  assert.equal(out.title, 'Calc')
  assert.ok(!calls.some((c) => c.name === 'hid.click'),
    'an off-screen point must not be clicked')
  assert.ok(logs.some((l) => /outside the declared screen/.test(l)),
    `expected a warning, got ${JSON.stringify(logs)}`)
})

await asyncTest('a re-read that answers with a different window is refused',
  async () => {
    // The re-read asks by title, and a title is neither stable nor unique:
    // if the target renames itself in those 450ms another window can answer
    // to the old one, and everything after this would be aimed at that one.
    const { locator } = locatorWith({
      'device.info': { json: { screen: { width: 1920, height: 1080, source: 'explicit' } } },
      'hid.click': { json: { ok: true, clicked: true } },
      'screen.windows': {
        json: { window: { ...ONTOP, pid: 999 } }, text: '', isError: false,
      },
    })
    await assert.rejects(
      () => locator.ensureReachable({ ...ONTOP, pid: 10, foreground: false }),
      (err) => err instanceof LocateError
        && /returned a different window \(pid 999, not 10\)/.test(err.message))
  })

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed === 0 ? 0 : 1)
