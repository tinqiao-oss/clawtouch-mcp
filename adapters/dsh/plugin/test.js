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
import { readFileSync } from 'node:fs'

import {
  calibrate, toImagePoint, toScreenPoint, resolvePoint, CalibrationError,
} from './lib/calibrate.js'
import { parseAnswer, VisionError } from './lib/vision.js'
import {
  assertOnTop, unmeasuredNote, outOfBoundsFix, renderWindowLine, sameWindow,
  LocateError, describeResult, notPressedNote, SIMULATED_NOTE,
} from './lib/locator.js'
import { McpStdioClient } from './lib/mcp-client.js'
import { Locator } from './lib/locator.js'
import { refusedCombo } from './lib/keyguard.js'
import { untypableChars, untypableMessage } from './lib/typing.js'
import { typeText, pressKey, scrollWheel } from './lib/actions.js'

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

// ── the one repaired malformation (qwen-vl-max, measured 2026-09-13) ──
//
// 15.8% of qwen-vl-max's single-target replies left "markers" open and ran
// straight into "target". The content was right every time; only the
// brace was missing. These pin the repair AND its limits — the negative
// cases matter more, because a repair that generalises is a guesser.

test('qwen\'s unclosed markers object is repaired — the reply seen in the wild', () => {
  // Verbatim from a benchmark run on the Windows Calculator.
  const out = parseAnswer('{"markers":{"tl":[14,15],"br":[298,526],'
    + '"target":{"found":true,"point":[43,350],"label":"数字键 7",'
    + '"confidence":0.99}}')
  assert.deepEqual(out.markers.tl, [14, 15])
  assert.deepEqual(out.markers.br, [298, 526])
  assert.equal(out.target.found, true)
  assert.deepEqual(out.target.point, [43, 350])
  assert.equal(out.target.confidence, 0.99)
  assert.equal(out.repaired, true)   // said out loud, so the rate stays countable
})

test('the repair covers the batch shape and either marker order', () => {
  const out = parseAnswer('{"markers":{"br":[3,4],"tl":[1,2],'
    + '"targets":[{"n":1,"found":true,"point":[5,6]},{"n":2,"found":false}]}', 2)
  assert.deepEqual(out.markers.tl, [1, 2])
  assert.deepEqual(out.markers.br, [3, 4])
  assert.deepEqual(out.targets[0].point, [5, 6])
  assert.equal(out.targets[1].found, false)
})

test('a fenced reply is repaired; one wrapped in commentary is not', () => {
  const shape = '{"markers":{"tl":[1,2],"br":[3,4],'
    + '"target":{"found":true,"point":[7,8]}}'
  const out = parseAnswer('```json\n' + shape + '\n```')
  assert.deepEqual(out.target.point, [7, 8])
  assert.equal(out.repaired, true)
  // Only the whole reply is ever repaired: extracting it from prose means
  // cutting text away, and what gets cut can be a second answer.
  assert.throws(() => parseAnswer('Here you go:\n' + shape + '\nHope that helps.'),
    VisionError)
})

test('escaped quotes inside a label are not mistaken for keys', () => {
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4],"target":{"found":true,'
    + '"point":[5,6],"label":"say \\"target\\": now"}}')
  assert.deepEqual(out.target.point, [5, 6])
  assert.equal(out.target.label, 'say "target": now')
  assert.equal(out.repaired, true)
})

test('other malformations still fail loudly — the repair does not generalise', () => {
  const T = '"target":{"found":true,"point":[5,6]}'
  const broken = [
    // exactly ONE brace missing, but not the markers one: the outer brace.
    // A "close any one brace" repair would accept this; ours must not.
    `{"markers":{"tl":[1,2],"br":[3,4]},${T}`,
    // one bracket missing inside the target's point
    '{"markers":{"tl":[1,2],"br":[3,4]},"target":{"found":true,"point":[5,6}}',
    // truncated mid-answer: nothing to close deterministically
    '{"markers":{"tl":[1,2],"br":[3,4]},"target":{"found":true,"point":[5,',
    // the known shape PLUS another missing brace: one brace does not fix it
    `{"markers":{"tl":[1,2],"br":[3,4],${T}`,
    // markers written as {x,y} objects and left open: not the measured shape
    `{"markers":{"tl":{"x":1,"y":2},"br":{"x":3,"y":4},${T}}`,
    // markers not exactly tl + br: renamed, duplicated, or with an extra key
    `{"markers":{"aa":[1,2],"bb":[3,4],${T}}`,
    `{"markers":{"tl":[1,2],"tl":[3,4],${T}}`,
    `{"markers":{"tl":[1,2],"br":[3,4],"mid":[9,9],${T}}`,
    `{"markers":{"tl":[1,2],"br":[3,4],"scale":1.0,${T}}`,
    // a marker that is not exactly a pair of numbers
    `{"markers":{"tl":["a","b"],"br":[3,4],${T}}`,
    `{"markers":{"tl":[1,2,999],"br":[3,4],${T}}`,
    // markers not the reply's first key: a sibling before it, or nested
    `{"note":"x","markers":{"tl":[1,2],"br":[3,4],${T}}`,
    `{"info":{"markers":{"tl":[1,2],"br":[3,4],${T}}}`,
    // a well-formed reply whose NESTED object has the known shape: the repair
    // must not reach in there (unanchored, it would click [5,6] here)
    '{"markers":{"tl":[1,2],"br":[3,4]},"info":{"markers":{"tl":[1,2],'
      + '"br":[3,4],"target":{"found":false}},"target":{"found":true,"point":[5,6]}}',
    // a second answer, however it is spelled — JSON.parse keeps the LAST
    // duplicate, so each of these would turn "not found" into a click:
    `{"markers":{"tl":[1,2],"br":[3,4],"target":{"found":false},${T}}`,
    `{"markers":{"tl":[1,2],"br":[3,4],"target":{"found":false},`
      + '"t\\u0061rget":{"found":true,"point":[5,6]}}',
    `{"markers":{"tl":[1,2],"br":[3,4],${T},`
      + '"targets":[{"n":1,"found":true,"point":[5,6]}]}',
    // ...or hidden behind the last '}', where a brace slice would drop it
    `{"markers":{"tl":[1,2],"br":[3,4],${T}},"target":null`,
    // duplicate fields inside the one answer: which "found", which "point"?
    '{"markers":{"tl":[1,2],"br":[3,4],'
      + '"target":{"found":false,"point":[5,6],"found":true}}',
    '{"markers":{"tl":[1,2],"br":[3,4],'
      + '"target":{"found":true,"point":[5,6],"point":[9,10]}}',
    // the known shape, but inside commentary: not the whole reply
    `Sure: {"markers":{"tl":[1,2],"br":[3,4],${T}}`,
  ]
  for (const reply of broken) {
    assert.throws(() => parseAnswer(reply), VisionError, reply)
  }
})

test('a well-formed reply is never touched by the repair', () => {
  const plain = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4]},'
    + '"target":{"found":true,"point":[5,6]}}')
  assert.equal(plain.repaired, false)
  // "target" nested inside markers is legal JSON: it parses as written, so
  // the top-level target is simply absent — no brace is moved to "fix" it.
  const out = parseAnswer('{"markers":{"tl":[1,2],"br":[3,4],"target":[5,6]}}')
  assert.equal(out.target.found, false)
  assert.equal(out.repaired, false)
  assert.deepEqual(out.markers.target, [5, 6])   // read where it was written
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
    'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
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
    'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
    'hid.click': { json: { ok: false, hint: 'no convergence' } },
  })
  await assert.rejects(
    () => locator.ensureReachable({ ...ONTOP, foreground: false }),
    (err) => err instanceof LocateError && /not confirmed/.test(err.message))
})

await asyncTest('a raise that left it covered is caught by re-reading',
  async () => {
    // The click may raise something else entirely; the state afterwards is
    // read back rather than assumed. Foreground is TRUE here so that the
    // occlusion guard is the only thing that can fail this — binding the
    // two conditions into one fixture is how the raise-did-not-take check
    // below went untested.
    const { locator } = locatorWith({
      'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
      'hid.click': { json: { ok: true, clicked: true } },
      'screen.windows': { json: { window: { ...ONTOP, foreground: true, visible_fraction: 0 } } },
    })
    await assert.rejects(
      () => locator.ensureReachable({ ...ONTOP, foreground: false, visible_fraction: 0 }),
      (err) => err instanceof LocateError && /in front of it/.test(err.message))
  })

await asyncTest('a raise the re-read says did not happen is a failure',
  async () => {
    // We clicked it ON PURPOSE to bring it forward. A re-read that says it
    // is still not in front is a failed action, not a detail: the next
    // click may be swallowed as activation instead of doing what it was
    // aimed at. Unoccluded, so only this check can fail it.
    const { locator } = locatorWith({
      'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
      'hid.click': { json: { ok: true, clicked: true } },
      'screen.windows': { json: { window: { ...ONTOP, foreground: false, visible_fraction: 1 } } },
    })
    await assert.rejects(
      () => locator.ensureReachable({ ...ONTOP, foreground: false, visible_fraction: 1 }),
      (err) => err instanceof LocateError && /the raise did not take/.test(err.message))
  })

await asyncTest('an UNMEASURED foreground does not fail the raise', async () => {
  // Absent is not false. Refusing where the platform cannot answer would
  // ground the plugin exactly where the question is unanswerable — the
  // same reason the occlusion guard lets an unmeasured window through.
  const fresh = { ...ONTOP, visible_fraction: 1 }
  delete fresh.foreground
  const { locator } = locatorWith({
    'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
    'hid.click': { json: { ok: true, clicked: true } },
    'screen.windows': { json: { window: fresh } },
  })
  const out = await locator.ensureReachable({ ...ONTOP, foreground: false, visible_fraction: 1 })
  assert.equal(out.title, 'Calc')
})

await asyncTest('a re-read that errors is not a raise that worked',
  async () => {
    // Falling back to the pre-click window here would be exactly the
    // assumption the re-read exists to replace, and the next screenshot
    // would be taken at a rectangle nobody confirmed.
    const { locator } = locatorWith({
      'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
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
    'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
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
  const MEASURED = { visible_fraction: 1, enabled: true, foreground: true }
  assert.equal(unmeasuredNote(MEASURED), '')
  assert.equal(unmeasuredNote(
    { visible_fraction: 0.4, enabled: false, foreground: false }), '')
  // macOS measures neither of those two, but DOES answer foreground.
  assert.match(unmeasuredNote({ title: 'Safari', foreground: false }),
    /input state and occlusion unmeasured/)
  assert.doesNotMatch(unmeasuredNote({ title: 'Safari', foreground: false }),
    /frontmost/)
  // Minimised on Windows, or a rectangle too small to sample: the input
  // state is known, the occlusion figure is not.
  assert.match(unmeasuredNote({ enabled: true, foreground: true }),
    /occlusion unmeasured/)
  assert.doesNotMatch(unmeasuredNote({ enabled: true, foreground: true }),
    /input state/)
  // ...and the reverse, so neither branch can quietly disappear.
  assert.match(unmeasuredNote({ visible_fraction: 1, foreground: true }),
    /input state unmeasured/)
  assert.doesNotMatch(unmeasuredNote({ visible_fraction: 1, foreground: true }),
    /occlusion/)
  // The frontmost query itself can fail. Normally it IS measured, so its
  // absence is the notable case — and `false` must never stand in for it.
  assert.match(unmeasuredNote({ visible_fraction: 1, enabled: true }),
    /which is frontmost unmeasured/)
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
          json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } },
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
  // ...and the remedy, not just the diagnosis. 7460 is past the right
  // edge, so widening --screen really is the answer here.
  assert.ok(logs.some((l) => /covering the whole virtual desktop/.test(l)),
    `expected the widen advice, got ${JSON.stringify(logs)}`)
})

await asyncTest('a NEGATIVE raise point gets the other advice', async () => {
  // The third call site of outOfBoundsFix, pinned the same way as the two
  // refusals: a Windows secondary monitor placed left of the primary has
  // negative coordinates too, and widening --screen cannot reach it.
  const logs = []
  const calls = []
  const mcp = {
    async callTool(name) {
      calls.push({ name })
      if (name === 'device.info') {
        return {
          json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } },
          text: '', images: [], isError: false,
        }
      }
      return { json: {}, text: '', images: [], isError: false }
    },
  }
  const locator = new Locator({
    mcp, config: {}, log: (level, m) => logs.push(`${level}:${m}`),
  })
  await locator.ensureReachable({
    ...ONTOP, foreground: false, raise_point: [-940, 8],
  })
  assert.ok(!calls.some((c) => c.name === 'hid.click'),
    'an off-screen point must not be clicked')
  assert.ok(logs.some((l) => /negative coordinate is out of range/.test(l)),
    `expected the negative advice, got ${JSON.stringify(logs)}`)
  assert.ok(!logs.some((l) => /covering the whole virtual desktop/.test(l)),
    'must not repeat the advice that cannot work')
})

await asyncTest('a re-read that answers with a different window is refused',
  async () => {
    // The re-read asks by title, and a title is neither stable nor unique:
    // if the target renames itself in those 450ms another window can answer
    // to the old one, and everything after this would be aimed at that one.
    const { locator } = locatorWith({
      'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
      'hid.click': { json: { ok: true, clicked: true } },
      'screen.windows': {
        json: { window: { ...ONTOP, pid: 999 } }, text: '', isError: false,
      },
    })
    await assert.rejects(
      () => locator.ensureReachable({ ...ONTOP, pid: 10, foreground: false }),
      (err) => err instanceof LocateError
        && /returned a different window/.test(err.message)
        && /pid 999/.test(err.message))
  })

await asyncTest('a SIBLING window of the same app is refused too', async () => {
  // pid alone was the first answer to "is this the same window", and two
  // windows of one application share it — so a sibling passed, and the
  // screenshot and every click after it went somewhere else. Raising does
  // not move a window, so a changed rect is the tell.
  const { locator } = locatorWith({
    'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
    'hid.click': { json: { ok: true, clicked: true } },
    'screen.windows': {
      json: { window: { ...ONTOP, pid: 10, rect: [600, 0, 900, 500] } },
      text: '', isError: false,
    },
  })
  await assert.rejects(
    () => locator.ensureReachable({
      ...ONTOP, pid: 10, rect: [0, 0, 300, 500], foreground: false }),
    (err) => err instanceof LocateError
      && /returned a different window/.test(err.message))
})

test('sameWindow compares only what both sides carry', () => {
  // An absent field is not evidence either way — the rule this whole
  // change is built on, applied to identity as well.
  assert.equal(sameWindow({ pid: 1, rect: [0, 0, 1, 1] },
    { pid: 1, rect: [0, 0, 1, 1] }), true)
  assert.equal(sameWindow({ pid: 1 }, { pid: 2 }), false)
  assert.equal(sameWindow({ pid: 1, rect: [0, 0, 1, 1] },
    { pid: 1, rect: [5, 0, 6, 1] }), false)
  assert.equal(sameWindow({ pid: 1 }, { pid: 1 }), true)
  assert.equal(sameWindow({ rect: [0, 0, 1, 1] }, { pid: 1 }), true)
})

// ── advice for a point outside the addressable screen ──────────────────
//
// Measured on macOS with the second display at origin (-1920, 0): the
// generic "widen --screen to the whole virtual desktop" is not merely
// unhelpful there, it is impossible — the flag takes a size and no
// origin — and a caller who follows it gets the identical failure a
// second time and concludes the tool is broken.

const BOUNDS = { width: 1512, height: 982 }

test('a point past the right edge gets the --screen advice', () => {
  const fix = outOfBoundsFix(1954, 814, BOUNDS)
  assert.match(fix, /--screen WxH covering the whole virtual desktop/)
  assert.doesNotMatch(fix, /negative coordinate/)
})

test('a negative x is NOT told to widen --screen', () => {
  const fix = outOfBoundsFix(-980, 420, BOUNDS)
  assert.match(fix, /negative coordinate is out of range/)
  // The whole point: it must not repeat the advice that cannot work.
  assert.doesNotMatch(fix, /covering the whole virtual desktop/)
})

test('a negative y is treated the same as a negative x', () => {
  assert.match(outOfBoundsFix(400, -12, BOUNDS),
    /negative coordinate is out of range/)
})

test('the negative half still says to widen --screen afterwards', () => {
  // Moving the display right of the primary makes the coordinate
  // positive and puts it PAST the old primary-only bounds, so
  // rearranging alone is not the whole fix.
  assert.match(outOfBoundsFix(-980, 420, BOUNDS),
    /give --screen a size that includes where it lands/)
})

test('a point that is BOTH negative and past an edge gets both halves',
  () => {
    // Negative x AND past the bottom edge: the two faults are
    // independent and suppressing either leaves the caller stuck.
    const fix = outOfBoundsFix(-10, 1400, BOUNDS)
    assert.match(fix, /negative coordinate is out of range/)
    assert.match(fix, /--screen WxH covering the whole virtual desktop/)
  })

test('zero is not negative — it takes the widen branch, not the other', () => {
  // Pins the branch boundary itself: a regression from `< 0` to `<= 0`
  // would start calling an addressable edge pixel unreachable.
  const fix = outOfBoundsFix(0, 0, { width: 0, height: 0 })
  assert.doesNotMatch(fix, /negative coordinate/)
  assert.match(fix, /--screen WxH/)
})

test('with no bounds at all the generic advice is a fallback, not an add-on',
  () => {
    assert.doesNotMatch(outOfBoundsFix(-980, 420, undefined),
      /covering the whole virtual desktop/)
    assert.match(outOfBoundsFix(9999, 9999, undefined),
      /covering the whole virtual desktop/)
  })

await asyncTest('the click refusal carries the negative advice and sends nothing',
  async () => {
    // A window on a display left of the primary: the point is refused
    // before anything is sent, and the message has to name the real fix.
    const { locator, calls } = locatorWith({
      'device.info': {
        json: { info: { connected: true }, screen: { width: 1512, height: 982, source: 'detected' } },
      },
    })
    locator.point = async () => ({ screen: [-980, 420] })
    await assert.rejects(
      () => locator.click({ target: 'the 7 key' }),
      (err) => err instanceof LocateError
        && /negative coordinate is out of range/.test(err.message)
        && !/covering the whole virtual desktop/.test(err.message))
    // The refusal is only worth anything if the click really did not go
    // out — asserting on the message alone would pass a broken guard.
    assert.equal(calls.filter((c) => c.name === 'hid.click').length, 0)
  })

await asyncTest('the multi-target refusal carries the same split', async () => {
  const { locator, calls } = locatorWith({
    'device.info': {
      json: { info: { connected: true }, screen: { width: 1512, height: 982, source: 'explicit' } },
    },
  })
  locator.pointMany = async () => ({
    results: [{ target: 'the 7 key', screen: [-980, 420] }],
  })
  await assert.rejects(
    () => locator.clickSequence({ targets: ['the 7 key'] }),
    (err) => err instanceof LocateError
      && /negative coordinate is out of range/.test(err.message))
  assert.equal(calls.filter((c) => c.name === 'hid.batch').length, 0)
})

// ── the region resolver when nothing reported itself as foreground ─────
//
// macOS answers `foreground` from a real query now, and a query that
// could not be made flags nothing rather than guessing. Falling back to
// the first window is still the most useful thing to do — but calling it
// the foreground window would re-tell, one layer up, exactly the guess
// the server stopped making, in a string the agent cannot check.

await asyncTest('a list with no foreground window is not called foreground',
  async () => {
    const { locator } = locatorWith({
      'screen.windows': {
        json: {
          windows: [
            { title: 'First', rect: [0, 0, 800, 600], foreground: false },
            { title: 'Second', rect: [0, 0, 400, 300], foreground: false },
          ],
        },
        text: '', images: [], isError: false,
      },
      'device.info': {
        json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } },
      },
    })
    const picked = await locator.resolveRegion({})
    assert.deepEqual(picked.region, [0, 0, 800, 600], 'still uses the first')
    assert.match(picked.source, /first listed window "First"/)
    assert.match(picked.source, /nothing reported itself as foreground/)
    assert.doesNotMatch(picked.source, /^foreground window/)
  })

await asyncTest('a flagged window is still reported as the foreground one',
  async () => {
    const { locator } = locatorWith({
      'screen.windows': {
        json: {
          windows: [
            { title: 'First', rect: [0, 0, 800, 600], foreground: false },
            { title: 'Real', rect: [0, 0, 400, 300], foreground: true },
          ],
        },
        text: '', images: [], isError: false,
      },
      'device.info': {
        json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } },
      },
    })
    const picked = await locator.resolveRegion({})
    assert.deepEqual(picked.region, [0, 0, 400, 300])
    assert.match(picked.source, /foreground window "Real"/)
    assert.doesNotMatch(picked.source, /nothing reported itself/)
  })

await asyncTest('a window whose foreground field is missing says so',
  async () => {
    // The query failed on the server side, so the field is gone rather
    // than false — the note has to name that alongside the other guards.
    const { locator } = locatorWith({
      'screen.windows': {
        json: { windows: [{ title: 'Only', rect: [0, 0, 800, 600] }] },
        text: '', images: [], isError: false,
      },
      'device.info': {
        json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } },
      },
    })
    const picked = await locator.resolveRegion({})
    assert.match(picked.source, /which is frontmost unmeasured here/)
  })

// ── computer_windows renders the three field shapes distinguishably ────
//
// The star can only ever say YES. A `foreground` that went missing used
// to render exactly like one measured as false — same blank margin — so
// the reader could not tell "not in front" from "nobody asked".

test('a missing foreground renders differently from a measured false', () => {
  const lines = [
    { title: 'win-all', foreground: true, width: 800, height: 600,
      visible_percent: 100, accepts_input: true },
    { title: 'mac-false', foreground: false, width: 400, height: 300 },
    { title: 'mac-unasked', width: 200, height: 100 },
  ].map(renderWindowLine)

  // Fully measured: a star and no note at all.
  assert.match(lines[0], /^\* win-all/)
  assert.doesNotMatch(lines[0], /NOT measured/)
  // macOS, measured as not-frontmost: the two guards are named, and the
  // frontmost answer is NOT among them — it was measured.
  assert.match(lines[1], /input state and occlusion NOT measured here/)
  assert.doesNotMatch(lines[1], /frontmost/)
  // macOS, frontmost query failed: it has to be named too, or this line
  // is indistinguishable from the one above it.
  assert.match(lines[2], /which is frontmost NOT measured here/)
  // ...and the three-item list reads as a list, not "a and b and c".
  assert.match(lines[2], /input state, occlusion and which is frontmost/)
})

test('an unmeasured window can never carry the star', () => {
  // The last gate on this change's whole point, in the layer the agent
  // actually reads. Loosening the star to `w.foreground !== false` puts
  // a `*` on a window whose own note says the answer was not measured —
  // one line asserting and denying the same fact. The star may only ever
  // mean a measured yes.
  const line = renderWindowLine({ title: 'Safari', width: 800, height: 600 })
  assert.doesNotMatch(line, /^\*/, 'no star without a measurement')
  assert.match(line, /which is frontmost NOT measured here/)

  // ...and the two adjacent states stay distinguishable from it.
  assert.match(
    renderWindowLine({ title: 'S', foreground: true, width: 1, height: 1,
      visible_percent: 100, accepts_input: true }), /^\* S/)
  assert.doesNotMatch(
    renderWindowLine({ title: 'S', foreground: false, width: 1, height: 1,
      visible_percent: 100, accepts_input: true }), /^\*/)
})

test('the other two measurements still speak for themselves', () => {
  assert.match(
    renderWindowLine({ title: 'blocked', foreground: true, width: 1, height: 1,
      visible_percent: 100, accepts_input: false }),
    /NOT accepting input, a modal dialog is over it/)
  assert.match(
    renderWindowLine({ title: 'covered', foreground: true, width: 1, height: 1,
      visible_percent: 30, accepts_input: true }),
    /only 30% visible/)
})

// ── a window lookup that FAILED is not an answer of "not there" ────────
//
// The server tells the two apart by sending `available` only on a genuine
// miss. Without that check, an unreadable listing was reported as measured
// absence — and, falling back to the FIRST listing's titles, produced
//   no visible window matching "Calc". Visible windows: "Calc"
// while discarding the server's own explanation.

const LISTED = {
  json: { windows: [{ title: 'Calc', pid: 42, rect: [0, 0, 400, 600] }] },
  text: '', images: [], isError: false,
}

await asyncTest('a genuine miss still names what IS there', async () => {
  const { locator } = locatorWith({
    'screen.windows': (calls) => (calls.length === 1 ? LISTED : {
      // Wire-accurate: a real miss comes back isError:true WITH
      // `available` (measured against the running server). So `available`
      // — not isError — is the discriminator, and a guard that rejected
      // isError first would misclassify every real miss as an unreadable
      // listing.
      json: { error: "no visible window matching 'Nope'", available: ['Calc'] },
      text: '', images: [], isError: true,
    }),
  })
  await assert.rejects(
    () => locator.resolveRegion({ window: 'Nope' }),
    (err) => err instanceof LocateError
      && /no visible window matching "Nope"/.test(err.message)
      && /Visible windows: "Calc"/.test(err.message))
})

await asyncTest('a failed lookup is not reported as absence', async () => {
  // What clawtouch-mcp returns when the second listing raises: an `error`
  // and NO `available`.
  const { locator } = locatorWith({
    'screen.windows': (calls) => (calls.length === 1 ? LISTED : {
      json: {
        error: 'macOS returned no window list at all (...gave NULL)',
        platform: 'darwin',
      },
      text: '', images: [], isError: true,
    }),
  })
  await assert.rejects(
    () => locator.resolveRegion({ window: 'Calc' }),
    (err) => err instanceof LocateError
      // never the absurdity of naming the window in the list it is
      // supposedly missing from
      && !/no visible window matching/.test(err.message)
      && /whether it is there is unknown/.test(err.message)
      // the server's explanation is the actionable part; keep it
      && /gave NULL/.test(err.message))
})

await asyncTest('an unparseable reply is treated the same way', async () => {
  const { locator } = locatorWith({
    'screen.windows': (calls) => (calls.length === 1 ? LISTED : {
      json: undefined, text: 'boom', images: [], isError: true,
    }),
  })
  await assert.rejects(
    () => locator.resolveRegion({ window: 'Calc' }),
    (err) => err instanceof LocateError
      && !/no visible window matching/.test(err.message)
      && /whether it is there is unknown/.test(err.message))
})

// ── a NAMED window is never answered by widening to the whole desktop ──

await asyncTest('an unreadable list refuses a named window', async () => {
  // Before the server stopped reporting an unreadable listing as an empty
  // desktop, this arrived as "no visible window matching X" and was
  // refused here. It has to keep being refused: capturing everything
  // answers a different question, and the model then locates in UI the
  // caller never mentioned.
  const { locator, calls } = locatorWith({
    'screen.windows': { json: { error: 'window server unreachable' },
      text: '', images: [], isError: true },
  })
  await assert.rejects(
    () => locator.resolveRegion({ window: 'Calc' }),
    (err) => err instanceof LocateError
      && /the window list could not be read/.test(err.message)
      && /window server unreachable/.test(err.message))
  assert.ok(!calls.some((c) => c.name === 'hid.screenshot'),
    'nothing may be captured')
})

await asyncTest('an unreadable list still widens when NO window was named',
  async () => {
    // The unnamed case keeps its fallback — there, the whole desktop is
    // still an answer to the question that was asked.
    const { locator } = locatorWith({
      'screen.windows': { json: { error: 'window server unreachable' },
        text: '', images: [], isError: true },
    })
    const picked = await locator.resolveRegion({})
    assert.equal(picked.source, 'full screen')
    assert.equal(picked.region, undefined)
  })

// ── key combos the guard refuses (lib/keyguard.js) ─────────────────────
//
// A real keystroke goes wherever focus is. These cover the call an agent
// actually made on 2026-09-13 (Alt+Tab, which took focus off the task
// window for good), the shorthand spelling that used to walk past the
// quit check, and the everyday combos that must keep working.

const refuse = (key, modifiers, platform = 'win32', opts = {}) =>
  refusedCombo(key, modifiers, { platform, ...opts })

test('Alt+Tab is refused however it is spelled', () => {
  for (const [key, mods] of [
    ['tab', ['alt']], ['Tab', ['Alt']], ['tab', ['alt', 'shift']],
    ['alt+tab', []], ['Alt+Tab', undefined], ['shift+alt+tab', []], ['tab', ['ALT ']],
  ]) {
    const hit = refuse(key, mods)
    assert.equal(hit?.kind, 'focus', `${JSON.stringify([key, mods])} got through`)
  }
})

test('the quit guard no longer misses the shorthand spelling', () => {
  // clawtouch-mcp splits "alt+f4" into key f4 held with alt; the old guard
  // compared `key` to "f4" and let this through
  assert.equal(refuse('alt+f4', [])?.kind, 'quit')
  assert.equal(refuse('cmd+q', [], 'darwin')?.kind, 'quit')
  assert.equal(refuse('gui+w', [], 'darwin')?.kind, 'quit')
  assert.equal(refuse('F4', ['alt'])?.kind, 'quit')
})

test('the other focus-switching combos are refused', () => {
  for (const [key, mods] of [
    ['esc', ['alt']], ['escape', ['ctrl']], ['esc', ['ctrl', 'shift']],
    ['delete', ['ctrl', 'alt']], ['tab', ['gui']], ['ctrl+esc', []],
  ]) {
    assert.equal(refuse(key, mods)?.kind, 'focus', `${JSON.stringify([key, mods])} got through`)
  }
})

test('on Windows every Windows-key combination is refused', () => {
  for (const key of ['d', 'r', 'l', 'e', '1', 'up', 'space']) {
    assert.equal(refuse(key, ['win'])?.kind, 'focus', `Win+${key} got through`)
    assert.equal(refuse(`win+${key}`, [])?.kind, 'focus', `win+${key} shorthand got through`)
  }
  assert.equal(refuse('d', ['gui'], 'linux')?.kind, 'focus')
})

test('on macOS Command is an application shortcut, except the ones that leave the app', () => {
  for (const key of ['c', 'v', 's', 'a', 'z', 'f']) {
    assert.equal(refuse(key, ['cmd'], 'darwin'), null, `Cmd+${key} was refused`)
  }
  for (const [key, mods] of [
    ['tab', ['cmd']], ['space', ['cmd']], [' ', ['cmd']], ['`', ['cmd']], ['h', ['cmd']],
    ['m', ['cmd']], ['esc', ['cmd', 'alt']], ['left', ['ctrl']],
  ]) {
    assert.equal(refuse(key, mods, 'darwin')?.kind, 'focus', `${JSON.stringify([key, mods])} got through`)
  }
})

test('ordinary keys and in-app combos go through', () => {
  for (const [key, mods] of [
    ['tab', []], ['tab', ['shift']], ['tab', ['ctrl']], ['esc', []], ['enter', []],
    ['c', ['ctrl']], ['v', ['ctrl']], ['s', ['ctrl', 'shift']], ['left', ['ctrl']],
    ['delete', []], ['delete', ['ctrl']], ['f4', []], ['q', []], ['+', []], ['ctrl+plus', []],
  ]) {
    assert.equal(refuse(key, mods), null, `${JSON.stringify([key, mods])} was refused`)
  }
})

test('each class is switched off on its own', () => {
  assert.equal(refuse('tab', ['alt'], 'win32', { focus: false }), null)
  assert.equal(refuse('f4', ['alt'], 'win32', { focus: false })?.kind, 'quit')
  assert.equal(refuse('f4', ['alt'], 'win32', { quit: false }), null)
  assert.equal(refuse('tab', ['alt'], 'win32', { quit: false })?.kind, 'focus')
})

test('arguments are read the way the plugin forwards them', () => {
  assert.equal(refuse(undefined, undefined), null)    // no key: the server rejects the call
  assert.equal(refuse('tab', 'alt'), null)            // non-array modifiers are sent as none: plain Tab
  assert.equal(refuse('tab', ['alt', 7])?.kind, 'focus')
  // the server stringifies the key: a numeric 1 held with Win is Win+1
  assert.equal(refuse(1, ['win'])?.kind, 'focus')
  assert.equal(refuse(42, ['alt']), null)             // "42" is no key at all
})

test('whitespace Python strips is stripped here too — the bypass the review found', () => {
  // The server strips names with Python's str.strip(), which also removes
  // U+001C-U+001F and U+0085; JavaScript's trim() does not. Each of these was
  // pressed by the server as the combo on the right while the guard saw an
  // unknown key.
  assert.equal(refuse('tab', ['alt'])?.kind, 'focus')      // Alt+Tab
  assert.equal(refuse('f4', ['alt'])?.kind, 'quit')        // Alt+F4
  assert.equal(refuse('d', ['gui'])?.kind, 'focus')        // Win+D
  assert.equal(refuse('alt+tab', [])?.kind, 'focus')       // shorthand tail
  assert.equal(refuse('　tab', ['alt'])?.kind, 'focus')      // both strip this one
  // JavaScript-only whitespace: the server would reject "﻿tab"; refusing it is harmless
  assert.equal(refuse('﻿tab', ['alt'])?.kind, 'focus')
})

test('Windows: Ctrl+W and Ctrl+F4 close like Cmd+W; Win+Q is the shell, not quit', () => {
  assert.equal(refuse('w', ['ctrl'])?.kind, 'quit')
  assert.equal(refuse('F4', ['control'])?.kind, 'quit')
  const winQ = refuse('q', ['win'])
  assert.equal(winQ?.kind, 'focus')
  assert.match(winQ.what, /^Win\+Q/)
  assert.equal(refuse('q', ['win'], 'win32', { focus: false }), null)
  assert.match(refuse('d', ['gui'], 'linux').what, /^Super\+D/)
})

test('macOS refuses only what does something there', () => {
  for (const [key, mods] of [['esc', ['alt']], ['esc', ['ctrl']], ['tab', ['alt']], ['w', ['ctrl']], ['delete', ['ctrl', 'alt']]]) {
    assert.equal(refuse(key, mods, 'darwin'), null, `${JSON.stringify([key, mods])} was refused on macOS`)
  }
  assert.equal(refuse('f2', ['ctrl'], 'darwin')?.kind, 'focus')   // keyboard focus to the menu bar
  assert.equal(refuse('F3', ['ctrl'], 'darwin')?.kind, 'focus')   // ... to the Dock
  assert.match(refuse('esc', ['cmd', 'option'], 'darwin').what, /Cmd\+Option\+Esc/)
})

// ── what computer_type can type ─────────────────────────────────────────
//
// The device types one key per character on a US layout and stops at the
// first character that has no key — after everything before it went out.
// So the whole text is checked before anything is sent.

test('plain ASCII is typeable; control characters are left to the server', () => {
  assert.deepEqual(untypableChars('Hello, world! 7+9=16 ~`|\\{}[]<>?'), [])
  // The server strips these itself and reports what it really sent.
  assert.deepEqual(untypableChars('line one\nline two\ttab\r\x7f'), [])
  assert.deepEqual(untypableChars(''), [])
})

test('non-ASCII is found before anything is sent, each character once', () => {
  assert.deepEqual(untypableChars('Hello，世界 世界'), ['，', '世', '界'])
  // What a model writes without being asked: curly quotes, a dash, an accent.
  assert.deepEqual(untypableChars('“quoted” — café'), ['“', '”', '—', 'é'])
  // These two look like whitespace and are not on the layout either.
  assert.deepEqual(untypableChars('a bc'), [' ', ''])
})

test('an emoji is one character, not two UTF-16 halves', () => {
  assert.deepEqual(untypableChars('ok 👍👍'), ['👍'])
})

test('the refusal says nothing happened and names what to fix', () => {
  const msg = untypableMessage(untypableChars('你好 hello'))
  assert.match(msg, /^nothing was typed or clicked/)
  assert.match(msg, /"你" "好"/)
  assert.match(untypableMessage([...'一二三四五六七八九十']), /and 2 more/)
})

// ── a server with no device, and a dry run, never read as a press ───────
//
// `clawtouch-mcp --mock` answers every action `ok` — `clicked: true`
// included — and presses nothing. `mock: true` is what someone without a
// board tries first, so relaying that as "clicked" is the worst place for
// this plugin to be wrong.

const MOCK_INFO = {
  json: {
    info: { port: '<mock>', connected: true, mock: true },
    screen: { width: 1920, height: 1080, source: 'explicit' },
  },
}
const REAL_INFO = {
  json: {
    info: { port: 'COM6', connected: true },
    screen: { width: 1920, height: 1080, source: 'explicit' },
  },
}
const LOCATED = {
  screen: [100, 200],
  source: 'window "Calc"',
  fit: { x: { scale: 1 }, y: { scale: 1 } },
  timings: { totalMs: 5 },
}

await asyncTest('a --mock click goes out but is not reported as a click', async () => {
  const { locator, calls } = locatorWith({
    'device.info': MOCK_INFO,
    'hid.click': { json: { ok: true, clicked: true } },
  })
  locator.point = async () => ({ ...LOCATED })
  const out = await locator.click({ target: 'the 7 key' })
  assert.equal(out.clicked, false)
  assert.equal(out.simulated, true)
  // Still sent — exercising that path is what --mock is for...
  assert.equal(calls.filter((c) => c.name === 'hid.click').length, 1)
  // ...and the device was asked about once, not once per question.
  assert.equal(calls.filter((c) => c.name === 'device.info').length, 1)
  assert.match(describeResult(out, 'would click'),
    /nothing was pressed: clawtouch-mcp is running with --mock/)
})

await asyncTest('a click on a real device is still a click', async () => {
  const { locator } = locatorWith({
    'device.info': REAL_INFO,
    'hid.click': { json: { ok: true, clicked: true } },
  })
  locator.point = async () => ({ ...LOCATED })
  const out = await locator.click({ target: 'the 7 key' })
  assert.equal(out.clicked, true)
  assert.equal(out.simulated, undefined)
  assert.equal(notPressedNote(out), '')
  assert.doesNotMatch(describeResult(out, 'clicked'), /nothing was pressed/)
})

await asyncTest('a --mock click sequence is not reported as clicks either', async () => {
  const { locator } = locatorWith({
    'device.info': MOCK_INFO,
    'hid.batch': { json: { ok: true, results: [{ ok: true }, { ok: true }] } },
  })
  locator.pointMany = async () => ({
    results: [
      { target: 'the 7 key', screen: [10, 10] },
      { target: 'the plus key', screen: [20, 10] },
    ],
  })
  const out = await locator.clickSequence({ targets: ['the 7 key', 'the plus key'] })
  assert.equal(out.clicked, false)
  assert.equal(notPressedNote(out), SIMULATED_NOTE)
})

await asyncTest('a --mock server is not asked to raise a window', async () => {
  // Its "click" would log, the re-read would say the raise did not take,
  // and the agent would be told something was holding focus.
  const { locator, calls } = locatorWith({ 'device.info': MOCK_INFO })
  const out = await locator.ensureReachable({ ...ONTOP, foreground: false })
  assert.equal(out.title, 'Calc')
  assert.ok(!calls.some((c) => c.name === 'hid.click'))
})

await asyncTest('dryRun does not click to raise a window', async () => {
  // "Locates and reports without pressing anything" — the title-bar click
  // that brings a window forward is a press, and in a browser it can open
  // a tab.
  const { locator, calls } = locatorWith({}, { dryRun: true })
  const out = await locator.ensureReachable({ ...ONTOP, foreground: false })
  assert.equal(out.title, 'Calc')
  assert.equal(calls.length, 0, 'nothing should have been sent')
})

await asyncTest('dryRun refuses a covered window and says why it was not raised',
  async () => {
    const { locator, calls } = locatorWith({}, { dryRun: true })
    await assert.rejects(
      () => locator.ensureReachable({ ...ONTOP, foreground: false, visible_fraction: 0 }),
      (err) => err instanceof LocateError
        && /in front of it/.test(err.message)
        && /No click was made to raise it: dryRun presses nothing/.test(err.message))
    assert.equal(calls.length, 0)
  })

await asyncTest('a dry-run click sends nothing and says nothing was pressed', async () => {
  const { locator, calls } = locatorWith({}, { dryRun: true })
  locator.point = async () => ({ ...LOCATED })
  const out = await locator.click({ target: 'the 7 key' })
  assert.equal(out.clicked, false)
  assert.equal(out.dryRun, true)
  assert.equal(calls.length, 0)
  assert.match(describeResult(out, 'would click'), /nothing was pressed: dryRun is on/)
})

await asyncTest('an unreadable device.info is refused, then asked again', async () => {
  // Not "a device" by default: guessing that is how a mock's clicks got
  // reported as clicks. And not remembered either.
  let n = 0
  const { locator, calls } = locatorWith({
    'device.info': () => (++n === 1
      ? { json: undefined, text: 'metadata unavailable', images: [], isError: true }
      : MOCK_INFO),
  })
  await assert.rejects(() => locator.simulated(),
    (err) => err instanceof LocateError && /could not be read/.test(err.message)
      && /nothing was sent/.test(err.message))
  assert.equal(await locator.simulated(), true, 'asked again, and believed')
  assert.equal(calls.filter((c) => c.name === 'device.info').length, 2)
})

await asyncTest('a reply of the wrong shape is not an answer', async () => {
  // Parseable is not enough: `{}` carries no `info`, so it says nothing
  // about the device — and must not be cached as if it did.
  for (const bad of [{}, [], { error: 'metadata unavailable' }, { info: [] }]) {
    let n = 0
    const { locator } = locatorWith({
      'device.info': () => (++n === 1
        ? { json: bad, text: JSON.stringify(bad), images: [], isError: false }
        : MOCK_INFO),
    })
    await assert.rejects(() => locator.simulated(), /could not be read/,
      `${JSON.stringify(bad)} was taken as an answer`)
    assert.equal(await locator.simulated(), true)
  }
})

await asyncTest('when the device cannot be established, the click is not sent',
  async () => {
    // First question an error, second one throws: the click must not go out
    // between them — asked after it, a failure would report failure for a
    // click that happened.
    let n = 0
    const { locator, calls } = locatorWith({
      'device.info': () => {
        n += 1
        if (n === 1) return { json: undefined, text: 'no', images: [], isError: true }
        throw new Error('server went away')
      },
      'hid.click': { json: { ok: true, clicked: true } },
    })
    locator.point = async () => ({ ...LOCATED })
    await assert.rejects(() => locator.click({ target: 'the 7 key' }), /server went away/)
    assert.deepEqual(calls.map((c) => c.name), ['device.info', 'device.info'])
  })

await asyncTest('nor is the click that would raise a window', async () => {
  const { locator, calls } = locatorWith({
    'device.info': { json: undefined, text: 'no', images: [], isError: true },
    'hid.click': { json: { ok: true, clicked: true } },
  })
  await assert.rejects(
    () => locator.ensureReachable({ ...ONTOP, foreground: false }),
    /could not be read/)
  assert.ok(!calls.some((c) => c.name === 'hid.click'), 'no raise click')
})

await asyncTest('tools asking at once share one device.info request', async () => {
  const { locator, calls } = locatorWith({ 'device.info': MOCK_INFO })
  await Promise.all([locator.simulated(), locator.screenBounds(), locator.simulated()])
  assert.equal(calls.filter((c) => c.name === 'device.info').length, 1)
})

await asyncTest('a mock this plugin started is a mock even when device.info fails', async () => {
  // The flag is known without asking, so a failed question cannot turn it
  // back into a device.
  const { locator } = locatorWith({
    'device.info': { json: undefined, text: 'boom', images: [], isError: true },
    'hid.click': { json: { ok: true, clicked: true } },
  }, { args: ['--allow-screenshot', '--mock'] })
  locator.point = async () => ({ ...LOCATED })
  const out = await locator.click({ target: 'the 7 key' })
  assert.equal(out.clicked, false)
  assert.equal(out.simulated, true)
})

// ── the tools that send input directly: type, key, scroll ───────────────

function handlerWith(replies, config = {}) {
  const { locator, calls } = locatorWith(replies, config)
  return { deps: { locator, config }, calls }
}

await asyncTest('computer_type on a --mock server says nothing was typed, and asks first',
  async () => {
    const { deps, calls } = handlerWith({
      'device.info': MOCK_INFO,
      'hid.type': { json: { ok: true, chars: 5 } },
    })
    const out = await typeText(deps, { text: 'hello' })
    assert.match(out.summary,
      /^would type 5 characters — nothing was pressed: clawtouch-mcp is running with --mock/)
    assert.deepEqual(calls.map((c) => c.name), ['device.info', 'hid.type'])
  })

await asyncTest('computer_type on a real device says what was typed and what was left out',
  async () => {
    // The server drops control characters (a newline must not submit a
    // draft); "typed 9" for a 10-character request has to say why.
    const { deps } = handlerWith({
      'device.info': REAL_INFO,
      'hid.type': { json: { ok: true, chars: 9 } },
    })
    const out = await typeText(deps, { text: 'line one\nX' })
    assert.equal(out.summary, 'typed 9 characters (1 control character such as a '
      + 'newline or tab left out — send Enter or Tab with computer_key)')
    const plain = handlerWith({
      'device.info': REAL_INFO,
      'hid.type': { json: { ok: true, chars: 5 } },
    })
    assert.equal((await typeText(plain.deps, { text: 'hello' })).summary, 'typed 5 characters')
  })

await asyncTest('computer_type refuses untypeable text outside a dry run, before any call',
  async () => {
    // `point` is not stubbed: if the refusal did not come first, the target
    // click would reach for the window list and this would see the call.
    const { deps, calls } = handlerWith({})
    await assert.rejects(
      () => typeText(deps, { text: 'Hello，世界', target: 'the message box' }),
      (err) => err instanceof LocateError
        && /^nothing was typed or clicked/.test(err.message))
    assert.equal(calls.length, 0)
  })

await asyncTest('computer_type in a dry run says so and sends nothing', async () => {
  const { deps, calls } = handlerWith({}, { dryRun: true })
  const out = await typeText(deps, { text: 'hello' })
  assert.equal(out.summary, 'would type 5 characters — nothing was pressed: dryRun is on')
  assert.equal(calls.length, 0)
})

await asyncTest('computer_key: a mock says nothing was pressed, a device says pressed',
  async () => {
    const mock = handlerWith({ 'device.info': MOCK_INFO, 'hid.key': { json: { ok: true } } })
    assert.match((await pressKey(mock.deps, { key: 'enter' })).summary,
      /^would press enter — nothing was pressed: clawtouch-mcp is running with --mock/)
    assert.deepEqual(mock.calls.map((c) => c.name), ['device.info', 'hid.key'])
    const real = handlerWith({ 'device.info': REAL_INFO, 'hid.key': { json: { ok: true } } })
    assert.equal((await pressKey(real.deps, { key: 'c', modifiers: ['ctrl'] })).summary,
      'pressed ctrl+c')
    const dry = handlerWith({}, { dryRun: true })
    assert.equal((await pressKey(dry.deps, { key: 'enter' })).summary,
      'would press enter — nothing was pressed: dryRun is on')
    assert.equal(dry.calls.length, 0)
  })

await asyncTest('computer_scroll: a mock says nothing was pressed, a device says scrolled',
  async () => {
    const mock = handlerWith({ 'device.info': MOCK_INFO, 'hid.scroll': { json: { ok: true } } })
    assert.match((await scrollWheel(mock.deps, { amount: -3 })).summary,
      /^would scroll -3 — nothing was pressed: clawtouch-mcp is running with --mock/)
    assert.deepEqual(mock.calls.map((c) => c.name), ['device.info', 'hid.scroll'])
    const real = handlerWith({ 'device.info': REAL_INFO, 'hid.scroll': { json: { ok: true } } })
    assert.equal((await scrollWheel(real.deps, { amount: 2 })).summary, 'scrolled 2')
    // The wire name is `delta`; `amount` was once passed through and the
    // server rejected every call.
    assert.deepEqual(real.calls.find((c) => c.name === 'hid.scroll').args, { delta: 2 })
    const dry = handlerWith({}, { dryRun: true })
    assert.equal((await scrollWheel(dry.deps, { amount: 1 })).summary,
      'would scroll 1 — nothing was pressed: dryRun is on')
    assert.equal(dry.calls.length, 0)
  })

await asyncTest('type and scroll with a target click it first, then send', async () => {
  for (const [run, args, wire] of [
    [typeText, { text: 'hi', target: 'the message box' }, 'hid.type'],
    [scrollWheel, { amount: -2, target: 'the list' }, 'hid.scroll'],
  ]) {
    const { deps, calls } = handlerWith({
      'device.info': REAL_INFO,
      'hid.click': { json: { ok: true, clicked: true } },
      [wire]: { json: { ok: true, chars: 2 } },
    })
    deps.locator.point = async () => ({ ...LOCATED })
    const out = await run(deps, args)
    assert.deepEqual(calls.map((c) => c.name), ['device.info', 'hid.click', wire])
    assert.match(out.summary, /^clicked \(100, 200\) in window "Calc".*; (typed 2 characters|scrolled -2)$/)
  }
})

await asyncTest('after a failed device.info question, the next key asks afresh', async () => {
  // A rejected question must not be handed to every later caller.
  let n = 0
  const { deps, calls } = handlerWith({
    'device.info': () => {
      n += 1
      if (n === 1) throw new Error('server went away')
      return REAL_INFO
    },
    'hid.key': { json: { ok: true } },
  })
  await assert.rejects(() => pressKey(deps, { key: 'enter' }), /server went away/)
  assert.equal((await pressKey(deps, { key: 'enter' })).summary, 'pressed enter')
  assert.deepEqual(calls.map((c) => c.name), ['device.info', 'device.info', 'hid.key'])
})

await asyncTest('a device.info question that fails before a key means no key went out',
  async () => {
    // Asked before sending, so an error can never stand for a key that was
    // in fact pressed — which the agent would then press again.
    const calls = []
    const locator = new Locator({
      config: {},
      mcp: {
        async callTool(name) {
          calls.push(name)
          if (name === 'device.info') throw new Error('server went away')
          return { json: { ok: true }, text: '', images: [], isError: false }
        },
      },
    })
    await assert.rejects(() => pressKey({ locator, config: {} }, { key: 'enter' }),
      /server went away/)
    assert.deepEqual(calls, ['device.info'])
  })

// ── the number TESTING-macos.md tells a tester to expect ───────────────
//
// It drifted three times in one change, and a stale one is worse than
// none: a tester who gets a different count reasonably concludes their
// checkout or environment is wrong.
//
// Deliberately NOT a test(): a test only sees the tests declared above
// it, so appending one below would leave the doc stale and the suite
// green. This runs last and does not count itself, so the number in the
// doc is exactly the number this script prints.
{
  const doc = readFileSync(
    new URL('./TESTING-macos.md', import.meta.url), 'utf8')
  const quoted = /Expected: `(\d+) passed, 0 failed`/.exec(doc)
  // Snapshot the total BEFORE recording a failure, or the number this
  // reports is one more than the number the doc should carry.
  const total = passed + failed
  if (!quoted || Number(quoted[1]) !== total) {
    failed += 1
    console.error('FAIL  TESTING-macos.md is out of date with this suite\n'
      + `      doc says ${quoted ? quoted[1] : '(no count found)'}, `
      + `this run has ${total}`)
  }
}

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed === 0 ? 0 : 1)
