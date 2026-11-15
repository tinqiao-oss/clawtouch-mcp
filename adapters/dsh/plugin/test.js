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

test('Alt+Space (the window menu: close or minimise next) is refused off macOS', () => {
  // 2026-11-14: an agent pressed Alt+Space then N and minimised its task window.
  for (const [key, mods, platform] of [
    ['space', ['alt'], 'win32'], ['alt+space', [], 'win32'], ['Space', ['ALT'], 'linux'],
    [' ', ['alt'], 'win32'],
  ]) {
    assert.equal(refuse(key, mods, platform)?.kind, 'quit', `${JSON.stringify([key, mods, platform])} got through`)
  }
  // Option+Space on a Mac types a character; Win+Space stays a focus refusal.
  assert.equal(refuse('space', ['alt'], 'darwin'), null)
  assert.equal(refuse('space', ['win'], 'win32')?.kind, 'focus')
  assert.equal(refuse('space', [], 'win32'), null)
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

// ── a retired vision model names the config line to change ────────────

await asyncTest('a 404 "Model not exist" says which config line to change', async () => {
  const { locate } = await import('./lib/vision.js')
  const realFetch = globalThis.fetch
  // The body DashScope sent for qwen-vl-max on 2026-11-14.
  globalThis.fetch = async () => new Response(
    '{"error":{"message":"Model not exist.","type":"invalid_request_error"}}',
    { status: 404 })
  try {
    await assert.rejects(
      () => locate({
        imageBase64: 'AA==', mimeType: 'image/jpeg', target: 'the 7 key',
        markerHint: '', config: { apiKey: 'k', model: 'gone-model' },
      }),
      (err) => err instanceof VisionError
        && /returned 404/.test(err.message)
        && /"gone-model" is not served/.test(err.message)
        && /vision\.model/.test(err.message))
  } finally {
    globalThis.fetch = realFetch
  }
})

async function sentBody(config) {
  const { locate } = await import('./lib/vision.js')
  const realFetch = globalThis.fetch
  let body
  globalThis.fetch = async (_url, init) => {
    body = JSON.parse(init.body)
    return new Response(JSON.stringify({ choices: [{ message: { content:
      '{"markers":{"tl":[1,1],"br":[9,9]},"target":{"found":false}}' } }] }))
  }
  try {
    await locate({ imageBase64: 'AA==', mimeType: 'image/jpeg', target: 'x',
      markerHint: '', config: { apiKey: 'k', ...config } })
  } finally {
    globalThis.fetch = realFetch
  }
  return body
}

await asyncTest('the default eye is sent with thinking off — 45 s median without it', async () => {
  const body = await sentBody({})
  assert.equal(body.model, 'qwen3.8-max')
  assert.equal(body.enable_thinking, false)
  // Naming the default model explicitly (as cordis.patch.yml does) is the same.
  assert.equal((await sentBody({ model: 'qwen3.8-max' })).enable_thinking, false)
})

await asyncTest('another model or endpoint gets no extra fields unless asked', async () => {
  assert.equal('enable_thinking' in await sentBody({ model: 'other-vl' }), false)
  assert.equal('enable_thinking' in await sentBody({
    endpoint: 'https://example.invalid/v1/chat/completions' }), false)
  const asked = await sentBody({ model: 'other-vl', extraBody: { top_k: 1 } })
  assert.equal(asked.top_k, 1)
})

await asyncTest('extraBody {} sends nothing extra; it cannot replace model or messages', async () => {
  assert.equal('enable_thinking' in await sentBody({ extraBody: {} }), false)
  const body = await sentBody({ extraBody: { model: 'evil', messages: [] } })
  assert.equal(body.model, 'qwen3.8-max')
  assert.equal(body.messages.length, 2)
})

await asyncTest('a non-object extraBody is refused before any request', async () => {
  await assert.rejects(() => sentBody({ extraBody: 'enable_thinking=false' }),
    (err) => err instanceof VisionError && /extraBody/.test(err.message))
})

await asyncTest('an ordinary 500 gets no model hint', async () => {
  const { locate } = await import('./lib/vision.js')
  const realFetch = globalThis.fetch
  globalThis.fetch = async () => new Response('upstream overloaded', { status: 500 })
  try {
    await assert.rejects(
      () => locate({
        imageBase64: 'AA==', mimeType: 'image/jpeg', target: 'x',
        markerHint: '', config: { apiKey: 'k' },
      }),
      (err) => /returned 500/.test(err.message) && !/vision\.model/.test(err.message))
  } finally {
    globalThis.fetch = realFetch
  }
})


// ── the eye: a dsh model route (lib/eye.js) ───────────────────────────

const eyeMod = await import('./lib/eye.js')
const { DshEye, EyeError, resolveRoute, chooseEffort, legacyKeys } = eyeMod
const { stopIfCancelled } = await import('./lib/locator.js')

/** Stand-in for the host's BlockAssembler: same surface the eye reads. */
class FakeAssembler {
  constructor() { this._text = ''; this._finish = null; this._usage = undefined }
  push(chunk) {
    if (chunk.type === 'text') this._text += chunk.text
    if (chunk.type === 'usage') this._usage = chunk.usage
    if (chunk.type === 'finish') this._finish = chunk.reason
  }
  // The real one reports {kind:'stop'} when no finish chunk ever came.
  get finish() { return this._finish ?? { kind: 'stop' } }
  get usage() { return this._usage }
  blocks() { return this._text ? [{ type: 'text', text: this._text }] : [] }
}

function fakeServices({ modalities = ['text', 'image'], efforts, chunks, prepareError } = {}) {
  const seen = { saved: [], prepared: [], streamed: [] }
  const llm = {
    async prepareCall(config, _signal, configure) {
      if (prepareError) throw new Error(prepareError)
      const info = { provider: config.provider, id: config.model,
        inputModalities: modalities === null ? undefined : modalities,
        ...(efforts ? { reasoning: { efforts } } : {}) }
      const controls = configure({ maxTokens: config.maxTokens, temperature: config.temperature }, info)
      seen.prepared.push({ config, controls })
      return {
        config: { provider: config.provider, model: config.model, ...controls },
        async *stream(options) {
          seen.streamed.push(options)
          for (const c of (chunks ?? [{ type: 'text', text: '{"ok":1}' }, { type: 'finish', reason: { kind: 'stop' } }])) {
            if (options.signal?.aborted) throw new Error('aborted')
            yield c
          }
        },
      }
    },
  }
  const attachments = {
    async saveImage(input) { seen.saved.push(input); return { id: 'ref1' } },
  }
  return { services: { llm, attachments, BlockAssembler: FakeAssembler }, seen }
}

const EXEC = { agent: { model: { current: () => ({ provider: 'deepseek-official', model: 'deepseek-flash' }) } } }

test('legacy 0.1.x keys are found by presence, even when empty', () => {
  assert.deepEqual(legacyKeys({ endpoint: '', model: 'x' }), ['endpoint'])
  assert.deepEqual(legacyKeys({ apiKey: undefined, extraBody: {} }), ['apiKey', 'extraBody'])
  assert.deepEqual(legacyKeys({ provider: 'a', model: 'b' }), [])
  assert.deepEqual(legacyKeys(undefined), [])
})

test('the route: both set wins, neither borrows the conversation, one alone is an error', () => {
  assert.deepEqual(resolveRoute({ provider: 'bailian', model: 'qwen3.8-max' }, EXEC),
    { provider: 'bailian', model: 'qwen3.8-max', from: 'config' })
  assert.deepEqual(resolveRoute({}, EXEC),
    { provider: 'deepseek-official', model: 'deepseek-flash', from: 'conversation' })
  assert.throws(() => resolveRoute({ model: 'qwen3.8-max' }, EXEC), /go together/)
  assert.throws(() => resolveRoute({}, {}), /no model to look with/)
})

test('effort: configured wins, else "off" when offered, else the lowest, else none', () => {
  assert.equal(chooseEffort('high', { reasoning: { efforts: [{ id: 'off' }] } }), 'high')
  assert.equal(chooseEffort(undefined, { reasoning: { efforts: [{ id: 'low' }, { id: 'off' }] } }), 'off')
  assert.equal(chooseEffort(undefined, { reasoning: { efforts: [{ id: 'minimal' }, { id: 'high' }] } }), 'minimal')
  assert.equal(chooseEffort(undefined, { reasoning: { efforts: ['low', 'high'] } }), 'low')
  assert.equal(chooseEffort(undefined, {}), undefined)
})

await asyncTest('a legacy config is refused before any service is touched', async () => {
  const { services, seen } = fakeServices()
  const eye = new DshEye({ vision: { endpoint: 'https://x', apiKey: 'k' } })
  eye.attach(services)
  await assert.rejects(() => eye.prepare(EXEC), (err) => err instanceof EyeError
    && /vision\.endpoint, vision\.apiKey/.test(err.message) && /Nothing was pressed/.test(err.message))
  assert.equal(seen.prepared.length, 0)
})

await asyncTest('no llm/attachments service: refused with a reason, not a crash', async () => {
  const eye = new DshEye({ vision: {} })
  await assert.rejects(() => eye.prepare(EXEC), /no model service/)
})

await asyncTest('a route that cannot see images is refused — dsh would send a text placeholder', async () => {
  for (const modalities of [['text'], null]) {  // null = not declared
    const { services } = fakeServices({ modalities })
    const eye = new DshEye({ vision: {} })
    eye.attach(services)
    await assert.rejects(() => eye.prepare(EXEC), (err) => err instanceof EyeError
      && /deepseek-official\/deepseek-flash/.test(err.message)
      && /input: \[text, image\]/.test(err.message))
  }
})

await asyncTest('prepare picks the effort, asks for temperature 0, and borrows the conversation route', async () => {
  const { services, seen } = fakeServices({ efforts: [{ id: 'off', name: 'Off' }, { id: 'high', name: 'High' }] })
  const eye = new DshEye({ vision: {} })
  eye.attach(services)
  const prepared = await eye.prepare(EXEC)
  assert.equal(prepared.route.provider, 'deepseek-official')
  assert.equal(prepared.effort, 'off')
  assert.equal(seen.prepared[0].config.temperature, 0)
})

async function askWith(chunks, signal) {
  const { services, seen } = fakeServices({ chunks })
  const eye = new DshEye({ vision: {} })
  eye.attach(services)
  const prepared = await eye.prepare(EXEC)
  const out = await eye.ask(prepared, { imageBase64: Buffer.from('JPEGBYTES').toString('base64'),
    mimeType: 'image/jpeg', system: 'S', instruction: 'I', signal })
  return { out, seen }
}

await asyncTest('a clean answer comes back, with the screenshot saved as an attachment', async () => {
  const { out, seen } = await askWith()
  assert.equal(out.text, '{"ok":1}')
  assert.equal(Buffer.from(seen.saved[0].data).toString(), 'JPEGBYTES')
  assert.equal(seen.saved[0].mediaType, 'image/jpeg')
  const msg = seen.streamed[0].messages[0]
  assert.deepEqual(msg.content[0], { type: 'image', attachment: { id: 'ref1' } })
  assert.equal(seen.streamed[0].system, 'S')
})

await asyncTest('JSON followed by an error finish is NOT used', async () => {
  await assert.rejects(() => askWith([
    { type: 'text', text: '{"markers":{"tl":[1,1],"br":[9,9]},"target":{"found":true,"point":[5,5]}}' },
    { type: 'finish', reason: { kind: 'error', failure: { message: 'connection reset' } } },
  ]), (err) => err instanceof EyeError && /connection reset/.test(err.message))
})

await asyncTest('a reply with no finish chunk is NOT used (the assembler would call it a stop)', async () => {
  await assert.rejects(() => askWith([{ type: 'text', text: '{"point":[1,2]}' }]),
    /without a final status/)
})

await asyncTest('a truncated reply is NOT used', async () => {
  await assert.rejects(() => askWith([
    { type: 'text', text: '{"markers":' }, { type: 'finish', reason: { kind: 'max-tokens' } },
  ]), /cut off/)
})

await asyncTest('an image-budget refusal says what to change', async () => {
  await assert.rejects(() => askWith([{ type: 'finish', reason: { kind: 'error',
    failure: { code: 'IMAGE_OFFLOAD_REQUIRED', message: 'x' } } }]), /maxWidth/)
})

await asyncTest('a cancelled call is NOT used', async () => {
  const ac = new AbortController(); ac.abort()
  await assert.rejects(() => askWith(undefined, ac.signal), /cancelled/)
})

test('stopIfCancelled refuses after an abort and passes otherwise', () => {
  const ac = new AbortController()
  stopIfCancelled(ac.signal, 'x')
  stopIfCancelled(undefined, 'x')
  ac.abort()
  assert.throws(() => stopIfCancelled(ac.signal, 'clicking'), /cancelled before clicking, which was not sent/)
})

await asyncTest('an eye that cannot work refuses BEFORE the raise click and the screenshot', async () => {
  // A window that would need raising: the old order clicked its title bar,
  // took the screenshot, and only then discovered the eye could not work.
  const { locator, calls } = locatorWith({
    'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
    'screen.windows': (c) => (c.at(-1).args.title
      ? { json: { window: { ...ONTOP, foreground: false } } }
      : { json: { windows: [{ ...ONTOP, foreground: false }] } }),
  })
  locator.eye = new DshEye({ vision: { apiKey: 'old' } })
  await assert.rejects(() => locator.click({ target: 'the 7 key', window: 'Calc', exec: EXEC }),
    (err) => err instanceof LocateError && /no longer reads/.test(err.message))
  assert.ok(!calls.some((c) => c.name === 'hid.click'), 'no raise click')
  assert.ok(!calls.some((c) => c.name === 'hid.screenshot'), 'no capture')
})

await asyncTest('a click cancelled after locating sends nothing', async () => {
  const { locator, calls } = locatorWith({
    'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
    'hid.click': { json: { ok: true, clicked: true } },
  })
  const ac = new AbortController()
  locator.point = async () => { ac.abort(); return { screen: [10, 10], source: 'x', fit: { x: { scale: 1 }, y: { scale: 1 } }, timings: { totalMs: 1 } } }
  await assert.rejects(() => locator.click({ target: 'the 7 key', signal: ac.signal }), /cancelled/)
  assert.equal(calls.filter((c) => c.name === 'hid.click').length, 0)
})

await asyncTest('a key press cancelled before sending sends nothing', async () => {
  const { locator, calls } = locatorWith({
    'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
  })
  const ac = new AbortController(); ac.abort()
  await assert.rejects(() => pressKey({ locator, config: {} }, { key: 'enter' }, { signal: ac.signal }), /cancelled/)
  assert.equal(calls.filter((c) => c.name === 'hid.key').length, 0)
})

await asyncTest('the eye that looked is named in the result line', async () => {
  const line = describeResult({ screen: [1, 2], source: 'window "Calc"', fit: { x: { scale: 1 }, y: { scale: 1 } },
    timings: { totalMs: 3 }, eye: { provider: 'bailian', model: 'qwen3.8-max' }, clicked: true })
  assert.match(line, /eye bailian\/qwen3\.8-max/)
})


// ── review round 3 (2026-11-14): cancel at the send, thin captures, reloads ──

await asyncTest('a call cancelled while the server (re)starts is not sent', async () => {
  const client = new McpStdioClient({ command: 'unused' })
  const ac = new AbortController()
  const sent = []
  client.start = async () => { ac.abort() }          // cancelled during start
  client._request = async (method, params) => { sent.push(params); return { content: [] } }
  await assert.rejects(() => client.callTool('hid.key', { key: 'enter' }, { signal: ac.signal }),
    /it was not sent/)
  assert.equal(sent.length, 0)
})

test('a thin capture in the 0-1000 frame calibrates (1600x48: y scale ~20.8)', () => {
  const markers = [{ id: 'tl', center: [10, 10] }, { id: 'br', center: [1590, 38] }]
  const reported = { tl: [6.25, 208.3], br: [993.75, 791.7] }   // the same points, 0-1000
  const fit = calibrate(markers, reported, { width: 1600, height: 48 })
  assert.ok(Math.abs(fit.y.scale - 1000 / 48) < 0.2, `y scale ${fit.y.scale}`)
})

await asyncTest('services detached mid-look give an EyeError, not a TypeError', async () => {
  const { services } = fakeServices()
  const eye = new DshEye({ vision: {} })
  eye.attach(services)
  const prepared = await eye.prepare(EXEC)
  const realSave = services.attachments.saveImage
  services.attachments.saveImage = async (input) => { eye.detach(); return realSave(input) }
  const out = await eye.ask(prepared, { imageBase64: 'AA==', mimeType: 'image/jpeg', system: 'S', instruction: 'I' })
  assert.equal(out.text, '{"ok":1}')              // the pinned services finished the call
  eye.detach()
  await assert.rejects(() => eye.ask(prepared, { imageBase64: 'AA==', mimeType: 'image/jpeg', system: 'S', instruction: 'I' }),
    (err) => err instanceof EyeError && /went away/.test(err.message))
})

await asyncTest('a cancel after the field was clicked keeps the record of that click', async () => {
  const { locator, calls } = locatorWith({
    'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
  })
  const ac = new AbortController()
  locator.click = async () => {
    ac.abort()
    return { clicked: true, screen: [5, 5], source: 'window "Calc"', fit: { x: { scale: 1 }, y: { scale: 1 } }, timings: { totalMs: 1 } }
  }
  await assert.rejects(() => typeText({ locator, config: {} }, { text: 'abc', target: 'the field' }, { signal: ac.signal }),
    (err) => /clicked \(5, 5\)/.test(err.message) && /then cancelled before typing, which was not sent/.test(err.message))
  assert.equal(calls.filter((c) => c.name === 'hid.type').length, 0)
})

await asyncTest('an eye failure after a raise click says the raise happened', async () => {
  const { locator } = locatorWith({
    'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
  })
  locator.eye = { prepare: async () => ({ route: { provider: 'p', model: 'm' } }),
    ask: async () => { throw new EyeError('the p/m eye failed: boom; the target was not clicked') } }
  locator.resolveRegion = async () => ({ region: [0, 0, 10, 10], source: 'window "Calc"', raised: true })
  locator.capture = async () => ({ meta: { markers: [], width: 10, height: 10 }, image: { data: 'AA==' } })
  await assert.rejects(() => locator.pointMany({ targets: ['x'] }),
    (err) => err instanceof LocateError && /target was not clicked/.test(err.message)
      && /already been brought forward with one title-bar click/.test(err.message))
})


await asyncTest('a cancel INSIDE the send after a field click still reports that click', async () => {
  for (const [fn, args] of [[typeText, { text: 'abc', target: 'f' }], [scrollWheel, { amount: -3, target: 'f' }]]) {
    const { locator } = locatorWith({
      'device.info': { json: { info: { connected: true }, screen: { width: 1920, height: 1080, source: 'explicit' } } },
    })
    locator.click = async () => ({ clicked: true, screen: [5, 5], source: 'window "Calc"',
      fit: { x: { scale: 1 }, y: { scale: 1 } }, timings: { totalMs: 1 } })
    locator.mcp.callTool = async (name) => {
      if (name === 'device.info') return { json: { info: { connected: true } }, text: '', images: [], isError: false }
      throw new Error(`cancelled before ${name} was sent; it was not sent`)   // as mcp-client throws
    }
    await assert.rejects(() => fn({ locator, config: {} }, args, {}),
      (err) => /clicked \(5, 5\)/.test(err.message) && /it was not sent/.test(err.message))
  }
})

await asyncTest('a failed screenshot store is an EyeError that says the target was not clicked', async () => {
  const { services } = fakeServices()
  services.attachments.saveImage = async () => { throw new Error('disk full') }
  const eye = new DshEye({ vision: {} })
  eye.attach(services)
  const prepared = await eye.prepare(EXEC)
  await assert.rejects(() => eye.ask(prepared, { imageBase64: 'AA==', mimeType: 'image/jpeg', system: 'S', instruction: 'I' }),
    (err) => err instanceof EyeError && /disk full/.test(err.message) && /target was not clicked/.test(err.message))
})

// ── the settings page's config view (lib/settings.js) ─────────────────

const settingsMod = await import('./lib/settings.js')

// What the bundle patch (cordis.patch.yml) gives the row before any profile
// override: the layer a save compares against.
const BUNDLE = { command: 'clawtouch-mcp', maxWidth: 1600, dryRun: false }
const SAVE = (over = {}) => ({
  revision: 'r', eye: { mode: 'conversation' }, dryRun: false, maxWidth: 1600,
  command: 'clawtouch-mcp', ...over,
})

test('settings: nothing configured reads as the defaults, eye follows the conversation', () => {
  assert.deepEqual(settingsMod.editableSettings({}), {
    eye: { mode: 'conversation' }, dryRun: false, maxWidth: 1600, command: 'clawtouch-mcp',
  })
  assert.deepEqual(settingsMod.editableSettings(BUNDLE), settingsMod.editableSettings({}))
})

test('settings: a route needs both halves; one alone reads as incomplete', () => {
  assert.deepEqual(settingsMod.editableSettings({ vision: { provider: 'bailian', model: 'qwen3.8-max' } }).eye,
    { mode: 'route', provider: 'bailian', model: 'qwen3.8-max' })
  assert.deepEqual(settingsMod.editableSettings({ vision: { model: 'qwen3.8-max' } }).eye,
    { mode: 'incomplete', provider: '', model: 'qwen3.8-max' })
})

test('settings: the 0.1.x API key is reported by name, never by value', () => {
  const raw = { vision: { endpoint: 'https://x', apiKey: 'sk-SECRET-123', extraBody: {} } }
  const shown = settingsMod.describeSettings(raw, { entryFound: true })
  assert.deepEqual(shown.legacy, ['endpoint', 'apiKey', 'extraBody'])
  assert.ok(!JSON.stringify(shown).includes('sk-SECRET-123'))
  assert.ok(!JSON.stringify(shown).includes('https://x'))
})

test('settings: a !!js expression anywhere the page reads makes it read-only', () => {
  for (const raw of [
    { __jsExpr: 'x' },
    { dryRun: { __jsExpr: 'x' } },
    { maxWidth: { __jsExpr: 'x' } },
    { command: { __jsExpr: 'x' } },
    { vision: { __jsExpr: 'x' } },
    { vision: { model: { __jsExpr: 'x' } } },
  ]) {
    const shown = settingsMod.describeSettings(raw, { entryFound: true })
    assert.equal(shown.editable, false, JSON.stringify(raw))
    assert.equal(shown.readOnly, 'expression')
  }
  // an expression in a key the page does not touch is not its business
  assert.equal(settingsMod.describeSettings({ env: { __jsExpr: 'x' } }, { entryFound: true }).editable, true)
})

test('settings: without its own row the page is read-only', () => {
  const shown = settingsMod.describeSettings(BUNDLE, { entryFound: false })
  assert.equal(shown.editable, false)
  assert.equal(shown.readOnly, 'editor')
})

test('settings: a save body is checked field by field', () => {
  const bad = (over) => settingsMod.parseSave(SAVE(over))
  assert.equal(settingsMod.parseSave(null), 'body')
  assert.equal(bad({ revision: 1 }), 'revision')
  assert.equal(bad({ eye: { mode: 'route', provider: '', model: 'm' } }), 'eye.provider')
  assert.equal(bad({ eye: { mode: 'route', provider: 'p', model: ' m' } }), 'eye.model')
  assert.equal(bad({ eye: { mode: 'incomplete' } }), 'eye.mode')
  assert.equal(bad({ dryRun: 'yes' }), 'dryRun')
  assert.equal(bad({ maxWidth: 100 }), 'maxWidth')
  assert.equal(bad({ maxWidth: 1600.5 }), 'maxWidth')
  assert.equal(bad({ command: '   ' }), 'command')
  assert.equal(bad({ command: 'a\nb' }), 'command')
  assert.equal(bad({ dropLegacy: 'yes' }), 'dropLegacy')
  assert.deepEqual(bad({ command: '  C:\\venv\\Scripts\\clawtouch-mcp.exe ' }).command,
    'C:\\venv\\Scripts\\clawtouch-mcp.exe')
})

test('settings: saving the defaults over the bundle row drops the override', () => {
  const next = settingsMod.parseSave(SAVE())
  const out = settingsMod.withSettings(BUNDLE, next, BUNDLE)
  assert.deepEqual(out, BUNDLE)
  assert.notEqual(out, BUNDLE) // a copy, not the inherited object itself
})

test('settings: a change writes only what differs from the defaults', () => {
  const next = settingsMod.parseSave(SAVE({ dryRun: true, eye: { mode: 'route', provider: 'bailian', model: 'qwen3.8-max' } }))
  assert.deepEqual(settingsMod.withSettings(BUNDLE, next, BUNDLE), {
    dryRun: true, vision: { provider: 'bailian', model: 'qwen3.8-max' },
  })
})

test('settings: keys the page does not edit survive a save', () => {
  const current = {
    dryRun: true, port: 'COM7', args: ['--no-abs-pointer'], allowFocusSwitchCombos: true,
    vision: { provider: 'bailian', model: 'qwen3.8-max', effort: 'high', timeoutMs: 90000 },
  }
  const next = settingsMod.parseSave(SAVE({ dryRun: false }))
  assert.deepEqual(settingsMod.withSettings(current, next, BUNDLE), {
    port: 'COM7', args: ['--no-abs-pointer'], allowFocusSwitchCombos: true,
    vision: { effort: 'high', timeoutMs: 90000 },
  })
})

test('settings: an ordinary save keeps the 0.1.x keys; only dropLegacy removes them', () => {
  const current = { vision: { endpoint: 'https://x', apiKey: 'sk-1', extraBody: { a: 1 } } }
  const keep = settingsMod.withSettings(current, settingsMod.parseSave(SAVE()), BUNDLE)
  assert.deepEqual(keep.vision, current.vision)
  const drop = settingsMod.withSettings(current, settingsMod.parseSave(SAVE({ dropLegacy: true })), BUNDLE)
  assert.deepEqual(drop, BUNDLE)
})

test('settings: a typical 0.1.x vision block (model alone + endpoint + key) cleans up to nothing', () => {
  const current = { vision: { endpoint: 'https://x/v1', apiKey: 'sk-1', model: 'qwen-vl-max' } }
  assert.equal(settingsMod.editableSettings(current).eye.mode, 'incomplete')
  const next = settingsMod.parseSave(SAVE({ dropLegacy: true }))
  assert.deepEqual(settingsMod.withSettings(current, next, BUNDLE), BUNDLE)
})

test('settings: the revision covers the vision block and the three settings, and reveals none of it', () => {
  const r = settingsMod.revisionOf
  const base = { dryRun: true, vision: { provider: 'p', model: 'm', apiKey: 'sk-one' } }
  assert.notEqual(r(base), r({ ...base, vision: { ...base.vision, apiKey: 'sk-two' } }))
  assert.notEqual(r(base), r({ ...base, vision: { ...base.vision, effort: 'high' } }))
  assert.notEqual(r(base), r({ ...base, maxWidth: 1200 }))
  // key order is not a change; keys the page does not rewrite are not in it
  assert.equal(r(base), r({ vision: { apiKey: 'sk-one', model: 'm', provider: 'p' }, dryRun: true }))
  assert.equal(r(base), r({ ...base, env: { A: '1' }, port: 'COM7' }))
  assert.match(r(base), /^[A-Za-z0-9_-]{43}$/)
  assert.ok(!r(base).includes('sk-one'))
})

test('settings: the row is found by its fiber in both of dsh\'s shapes, and only if unique', () => {
  const fiber = {}
  const wrapped = Object.create(fiber)
  wrapped.ctx = { fiber }
  const editor = (rows) => ({ entries: () => rows })
  assert.equal(settingsMod.ownEntry(editor([{ fiber }]), fiber)?.fiber, fiber)
  assert.equal(settingsMod.ownEntry(editor([{ fiber: wrapped }]), fiber)?.fiber, wrapped)
  assert.equal(settingsMod.ownEntry(editor([{ fiber }, { fiber: wrapped }]), fiber), undefined)
  assert.equal(settingsMod.ownEntry(editor([{ fiber: {} }]), fiber), undefined)
  assert.equal(settingsMod.ownEntry({ entries: () => { throw new Error('x') } }, fiber), undefined)
})

test('settings: only routes that declare image input are offered', () => {
  assert.deepEqual(settingsMod.imageRoutes([
    { provider: 'bailian', id: 'qwen3.8-max', name: 'Qwen 3.8 Max', inputModalities: ['text', 'image'] },
    { provider: 'deepseek', id: 'deepseek-pro', name: '', inputModalities: ['text'] },
    { provider: 'x', id: 'unknown' },
  ]), [{ provider: 'bailian', model: 'qwen3.8-max', name: 'Qwen 3.8 Max' }])
})

// ── the activity gate (lib/activity.js) ───────────────────────────────

const { ActivityGate } = await import('./lib/activity.js')

await asyncTest('gate: a save cannot close it while a tool runs, and a closed gate refuses tools', async () => {
  const gate = new ActivityGate()
  let release
  const running = gate.run(() => new Promise((r) => { release = r }))
  assert.equal(gate.active, 1)
  assert.equal(gate.closeIfIdle('saving'), null)
  release('done')
  assert.equal(await running, 'done')
  const reopen = gate.closeIfIdle('saving its settings')
  assert.equal(typeof reopen, 'function')
  await assert.rejects(gate.run(async () => 'pressed'), /saving its settings.*refused before anything was pressed/)
  reopen()
  assert.equal(await gate.run(async () => 'pressed'), 'pressed')
})

await asyncTest('gate: closed for good stays closed, whatever reopens it', async () => {
  const gate = new ActivityGate()
  const reopen = gate.closeIfIdle('saving')
  gate.closeForGood('reloading')
  reopen()
  assert.equal(gate.idle, false)
  await assert.rejects(gate.run(async () => 1), /reloading/)
})

// ── the page's routes (lib/routes.js) ─────────────────────────────────

const routesMod = await import('./lib/routes.js')

function pageFixture({ config = {}, inherited = { command: 'clawtouch-mcp', maxWidth: 1600, dryRun: false }, edit } = {}) {
  const fiber = {}
  const row = { fiber, options: { config } }
  const writes = []
  const editor = {
    entries: () => [row],
    edit: edit ?? (async (entry, fn) => {
      const next = fn(entry.options.config, inherited)
      writes.push(next)
      entry.options.config = next
    }),
  }
  const activity = new ActivityGate()
  const route = routesMod.settingsRoute({ editor: () => editor, fiber, config, activity, graceMs: 20 })
  const get = async () => (await route.fetch(new Request('http://dsh/api', { method: 'GET' }))).json()
  const post = async (body) => {
    const res = await route.fetch(new Request('http://dsh/api', { method: 'POST', body: JSON.stringify(body) }))
    return { status: res.status, body: await res.json() }
  }
  return { row, writes, activity, get, post }
}
const FORM = { eye: { mode: 'conversation' }, dryRun: false, maxWidth: 1600, command: 'clawtouch-mcp' }

await asyncTest('routes: GET names the 0.1.x keys and never their values', async () => {
  const page = pageFixture({ config: { vision: { endpoint: 'https://old', apiKey: 'sk-SECRET' } } })
  const shown = await page.get()
  assert.deepEqual(shown.legacy, ['endpoint', 'apiKey'])
  assert.equal(shown.editable, true)
  assert.ok(!JSON.stringify(shown).includes('sk-SECRET'))
  assert.ok(!JSON.stringify(shown).includes('https://old'))
})

await asyncTest('routes: a save writes, then keeps tools out until the reload (or the grace period) is over', async () => {
  const page = pageFixture()
  const { revision } = await page.get()
  const res = await page.post({ ...FORM, dryRun: true, revision })
  assert.deepEqual(res, { status: 200, body: { ok: true } })
  // only what differs from the plugin's defaults is written
  assert.deepEqual(page.writes, [{ dryRun: true }])
  assert.equal(page.activity.idle, false)
  await new Promise((r) => setTimeout(r, 40))
  assert.equal(page.activity.idle, true)
})

await asyncTest('routes: no save while a tool runs; nothing is written', async () => {
  const page = pageFixture()
  const { revision } = await page.get()
  let release
  const tool = page.activity.run(() => new Promise((r) => { release = r }))
  const res = await page.post({ ...FORM, dryRun: true, revision })
  assert.deepEqual(res, { status: 409, body: { ok: false, error: 'busy' } })
  assert.deepEqual(page.writes, [])
  release()
  await tool
})

await asyncTest('routes: a tool that starts while a save is being written is refused', async () => {
  let finish
  const page = pageFixture({ edit: (entry, fn) => new Promise((r) => { finish = () => { fn(entry.options.config, {}); r() } }) })
  const { revision } = await page.get()
  const saving = page.post({ ...FORM, dryRun: true, revision })
  await new Promise((r) => setTimeout(r, 5))
  await assert.rejects(page.activity.run(async () => 'pressed'), /refused before anything was pressed/)
  finish()
  assert.equal((await saving).status, 200)
})

await asyncTest('routes: a stale revision is refused and the gate opens again at once', async () => {
  const page = pageFixture()
  const { revision } = await page.get()
  page.row.options.config = { dryRun: true } // edited by hand meanwhile
  const res = await page.post({ ...FORM, revision })
  assert.deepEqual(res, { status: 409, body: { ok: false, error: 'stale' } })
  assert.equal(page.activity.idle, true)
})

await asyncTest('routes: an editor failure is a code, never its message (YAML errors quote the profile)', async () => {
  const page = pageFixture({ edit: async () => { throw new Error('bad indentation:\n vision: {apiKey: sk-LEAK}\n') } })
  const { revision } = await page.get()
  const res = await page.post({ ...FORM, revision })
  assert.deepEqual(res, { status: 500, body: { ok: false, error: 'failed' } })
  const over = pageFixture({ edit: async () => { throw new Error('value is overridden by a home patch (sk-LEAK)') } })
  const r2 = await over.post({ ...FORM, revision: (await over.get()).revision })
  assert.deepEqual(r2, { status: 409, body: { ok: false, error: 'overridden' } })
})

await asyncTest('routes: a bad body is refused by field before anything else', async () => {
  const page = pageFixture()
  const res = await page.post({ ...FORM, maxWidth: 'wide', revision: 'x' })
  assert.deepEqual(res, { status: 400, body: { ok: false, error: 'invalid', field: 'maxWidth' } })
  assert.equal(page.activity.idle, true)
})

await asyncTest('routes: the eye check reads dsh\'s model info and reports failures as codes', async () => {
  const route = { vision: { provider: 'bailian', model: 'qwen3.8-max' } }
  assert.deepEqual(await routesMod.checkEye({}, undefined), { mode: 'conversation', legacy: [] })
  const seeing = { resolveModelInfo: async () => ({ inputModalities: ['text', 'image'] }) }
  assert.equal((await routesMod.checkEye(route, seeing)).image, true)
  const blind = { resolveModelInfo: async () => ({ inputModalities: ['text'] }) }
  assert.equal((await routesMod.checkEye(route, blind)).image, false)
  const leaky = { resolveModelInfo: async () => { throw new Error('no provider bailian (key sk-LEAK)') } }
  const failed = await routesMod.checkEye(route, leaky)
  assert.deepEqual([failed.image, failed.problem], [null, 'failed'])
  assert.ok(!JSON.stringify(failed).includes('sk-LEAK'))
  let aborted = false
  const slow = { resolveModelInfo: (p, m, signal) => new Promise(() => { signal.addEventListener('abort', () => { aborted = true }) }) }
  const late = await routesMod.checkEye(route, slow, 10)
  assert.deepEqual([late.image, late.problem, aborted], [null, 'timeout', true])
})

function fakeMcp({ tools = ['device.status', 'device.info'], answers = {}, startError, version = '0.6.1' } = {}) {
  const calls = []
  return {
    calls,
    running: false,
    serverInfo: null,
    async start() {
      if (startError) throw new Error(startError)
      this.running = true
      this.serverInfo = { name: 'clawtouch-mcp', version }
    },
    hasTool: (name) => tools.includes(name),
    async callTool(name) {
      calls.push(name)
      const a = answers[name]
      if (typeof a === 'function') return a()
      return { isError: false, text: JSON.stringify(a ?? {}), json: a ?? {} }
    },
  }
}

await asyncTest('routes: the device check is skipped, not queued, while a tool runs', async () => {
  const gate = new ActivityGate()
  let release
  const tool = gate.run(() => new Promise((r) => { release = r }))
  const mcp = fakeMcp()
  const out = await routesMod.checkDevice(mcp, 'clawtouch-mcp', gate)
  assert.equal(out.board.state, 'busy')
  assert.deepEqual(mcp.calls, [])
  release()
  await tool
})

await asyncTest('routes: a tool that starts while the server does means the check is not sent', async () => {
  const gate = new ActivityGate()
  const mcp = fakeMcp()
  let release
  let tool
  const realStart = mcp.start.bind(mcp)
  mcp.start = async () => {
    tool = gate.run(() => new Promise((r) => { release = r })) // the agent begins meanwhile
    await realStart()
  }
  const out = await routesMod.checkDevice(mcp, 'c', gate)
  assert.equal(out.board.state, 'busy')
  assert.deepEqual(mcp.calls, [])
  release()
  await tool
})

await asyncTest('routes: a start that outlasts the check still gets its idle release armed', async () => {
  const gate = new ActivityGate()
  const mcp = fakeMcp()
  let finish
  mcp.start = () => new Promise((r) => { finish = () => { mcp.running = true; r() } })
  const out = await routesMod.checkDevice(mcp, 'c', gate, 10)
  assert.equal(out.board.state, 'no-answer')
  assert.deepEqual(mcp.calls, [])
  finish()
  await new Promise((r) => setTimeout(r, 5))
  assert.deepEqual(mcp.calls, ['device.info'])
})

await asyncTest('routes: a connected board is reported with its firmware and pointer', async () => {
  const mcp = fakeMcp({ answers: { 'device.status': {
    mode: 'serial', connected: true, responding: true, port: 'COM22',
    firmware: { version: '1.2.0', board: 'pico2', caps: ['abs'] },
    abs_pointer: { firmware: true, enabled: true, reason: null },
  } } })
  const out = await routesMod.checkDevice(mcp, 'clawtouch-mcp', new ActivityGate())
  assert.deepEqual(out, {
    mcp: { found: true, command: 'clawtouch-mcp', version: '0.6.1' },
    board: { state: 'connected', port: 'COM22', firmware: '1.2.0', board: 'pico2', abs: { enabled: true, reason: null } },
  })
})

await asyncTest('routes: no board, mock and a silent board each say so', async () => {
  const gate = new ActivityGate()
  const absent = await routesMod.checkDevice(fakeMcp({ answers: { 'device.status': {
    mode: 'serial', connected: false, detail: 'No ClawTouch HID board was found', tried_ports: [] } } }), 'c', gate)
  assert.equal(absent.board.state, 'absent')
  const mock = await routesMod.checkDevice(fakeMcp({ answers: { 'device.status': { mode: 'mock' } } }), 'c', gate)
  assert.equal(mock.board.state, 'mock')
  const silent = await routesMod.checkDevice(fakeMcp({ answers: { 'device.status': {
    mode: 'serial', connected: true, responding: false, port: 'COM3', detail: 'ACK timeout' } } }), 'c', gate)
  assert.deepEqual(silent.board, { state: 'silent', port: 'COM3', detail: 'ACK timeout' })
})

await asyncTest('routes: a server that cannot be run is "not found", with the reason', async () => {
  const out = await routesMod.checkDevice(fakeMcp({ startError: 'could not run "clawtouch-mcp": spawn clawtouch-mcp ENOENT' }),
    'clawtouch-mcp', new ActivityGate())
  assert.equal(out.mcp.found, false)
  assert.match(out.mcp.detail, /ENOENT/)
})

await asyncTest('routes: an older clawtouch-mcp gets device.info, which arms its idle release', async () => {
  const mcp = fakeMcp({ tools: ['device.info'], version: '0.6.0',
    answers: { 'device.info': { info: { port: 'COM22', connected: true } } } })
  const out = await routesMod.checkDevice(mcp, 'clawtouch-mcp', new ActivityGate())
  assert.deepEqual(mcp.calls, ['device.info'])
  assert.deepEqual(out.board, { state: 'old-server', port: 'COM22' })
  assert.equal(out.mcp.version, '0.6.0')
})

await asyncTest('routes: a device that never answers is reported, and the client is left alone', async () => {
  const mcp = fakeMcp({ answers: { 'device.status': () => new Promise(() => {}) } })
  const out = await routesMod.checkDevice(mcp, 'c', new ActivityGate(), 10)
  assert.equal(out.board.state, 'no-answer')
})

await asyncTest('routes: two status requests at once make one check', async () => {
  let release
  const mcp = fakeMcp({ answers: { 'device.status': () => new Promise((r) => {
    release = () => r({ isError: false, json: { mode: 'mock' } })
  }) } })
  const route = routesMod.statusRoute({ editor: () => undefined, fiber: {}, config: {}, llm: () => undefined,
    mcp, command: 'c', activity: new ActivityGate() })
  const a = route.fetch()
  const b = route.fetch()
  await new Promise((r) => setTimeout(r, 5))
  release()
  const [ra, rb] = await Promise.all([a, b])
  assert.equal((await ra.json()).board.state, 'mock')
  assert.equal((await rb.json()).board.state, 'mock')
  assert.deepEqual(mcp.calls, ['device.status'])
})

await asyncTest('routes: models are listed per provider, image routes only, a slow provider named', async () => {
  const llm = {
    listProviders: () => [{ id: 'bailian', name: 'Bailian' }, { id: 'slow', name: 'Slow' }, { id: 'deepseek', name: '' }],
    listModels: async (id) => {
      if (id === 'slow') return new Promise(() => {})
      if (id === 'bailian') return [{ provider: 'bailian', id: 'qwen3.8-max', name: 'Qwen', inputModalities: ['text', 'image'] }]
      return [{ provider: 'deepseek', id: 'deepseek-flash', name: 'Flash', inputModalities: ['text'] }]
    },
  }
  const out = await routesMod.listImageRoutes(llm, 10)
  assert.deepEqual(out, {
    routes: [{ provider: 'bailian', model: 'qwen3.8-max', name: 'Qwen', providerName: 'Bailian' }],
    failed: ['slow'],
    available: true,
  })
})

test('routes: every route declares a buffered body (dsh answers 400 to a GET without one)', () => {
  const deps = { editor: () => undefined, fiber: {}, config: {}, llm: () => undefined, mcp: fakeMcp(),
    command: 'c', activity: new ActivityGate() }
  for (const route of [routesMod.settingsRoute(deps), routesMod.statusRoute(deps), routesMod.modelsRoute(deps)]) {
    assert.equal(route.requestBody, 'buffered', route.path)
    assert.match(route.path, /^\/api\/clawtouch\//)
  }
})

await asyncTest('routes: the model list is shared while fresh', async () => {
  let asked = 0
  const llm = { listProviders: () => [{ id: 'p', name: 'P' }], listModels: async () => { asked += 1; return [] } }
  const route = routesMod.modelsRoute({ llm: () => llm, freshMs: 60_000 })
  await Promise.all([route.fetch(), route.fetch()])
  await route.fetch()
  assert.equal(asked, 1)
})

// ── the MCP client's lifecycle ─────────────────────────────────────────

await asyncTest('mcp client: a second caller waits for the handshake, not just the spawn', async () => {
  const client = new McpStdioClient({ command: 'nothing' })
  let finish
  client._start = async () => {
    client.child = { exitCode: null } // spawned: `running` is already true
    await new Promise((r) => { finish = r })
    client.tools = [{ name: 'device.status' }]
  }
  const first = client.start()
  let secondDone = false
  const second = client.start().then(() => { secondDone = true })
  await new Promise((r) => setTimeout(r, 5))
  assert.equal(secondDone, false)
  finish()
  await Promise.all([first, second])
  assert.equal(client.hasTool('device.status'), true)
})

await asyncTest('mcp client: a failed handshake does not leave the server holding the port', async () => {
  const client = new McpStdioClient({ command: 'nothing' })
  let stopped = 0
  client._start = async () => {
    client.child = { exitCode: null }
    throw new Error('initialize timed out after 30000ms')
  }
  client.stop = async () => { stopped += 1; client.child = null }
  await assert.rejects(client.start(), /initialize timed out/)
  assert.equal(stopped, 1)
})

await asyncTest('mcp client: once closed it starts nothing and sends nothing', async () => {
  const client = new McpStdioClient({ command: 'nothing' })
  let spawned = 0
  client._start = async () => { spawned += 1 }
  await client.close()
  await assert.rejects(client.start(), /reloaded or shut down/)
  await assert.rejects(client.callTool('hid.click', {}), /reloaded or shut down/)
  assert.equal(spawned, 0)
})

// ── installing clawtouch-mcp (lib/installer.js) ───────────────────────

const inst = await import('./lib/installer.js')

test('installer: Windows candidates — launcher, python.org folders, PATH — never WindowsApps', () => {
  const files = new Set([
    'C:\\Windows\\py.exe',
    'C:\\Users\\u\\AppData\\Local\\Programs\\Python\\Python312\\python.exe',
    'C:\\Users\\u\\AppData\\Local\\Programs\\Python\\Python310\\python.exe',
    'C:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe',
    'C:\\tools\\conda\\python.exe',
  ])
  const dirs = { 'C:\\Users\\u\\AppData\\Local\\Programs\\Python': ['Python310', 'Python312', 'Launcher'] }
  const out = inst.candidates({
    platform: 'win32',
    env: { SystemRoot: 'C:\\Windows', LOCALAPPDATA: 'C:\\Users\\u\\AppData\\Local',
      PATH: 'C:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps;C:\\tools\\conda' },
    exists: (p) => files.has(p),
    list: (d) => dirs[d] ?? [],
  })
  assert.deepEqual(out.map((c) => [c.command, c.args.join(' ')]), [
    ['C:\\Windows\\py.exe', '-3'],
    ['C:\\Users\\u\\AppData\\Local\\Programs\\Python\\Python312\\python.exe', ''],
    ['C:\\Users\\u\\AppData\\Local\\Programs\\Python\\Python310\\python.exe', ''],
    ['C:\\tools\\conda\\python.exe', ''],
  ])
})

test('installer: macOS runs /usr/bin/python3 only when the active developer directory has a Python', () => {
  const run = (developerDir) => inst.candidates({
    platform: 'darwin', env: { PATH: '/usr/bin:/opt/homebrew/bin' }, developerDir,
    exists: (p) => p === '/usr/bin/python3' || p === '/opt/homebrew/bin/python3'
      || p === '/Applications/Xcode.app/Contents/Developer/usr/bin/python3',
    list: () => [],
  }).map((c) => c.command)
  assert.deepEqual(run(null), ['/opt/homebrew/bin/python3'])
  assert.deepEqual(run('/Library/Developer/CommandLineTools'), ['/opt/homebrew/bin/python3'])
  assert.deepEqual(run('/Applications/Xcode.app/Contents/Developer'), ['/opt/homebrew/bin/python3', '/usr/bin/python3'])
})

function probeAnswer(info) {
  return { code: 0, out: `${JSON.stringify({ venv: true, invenv: false, pip: true, mss: false, quartz: false,
    mcp: null, scripts: [], ...info })}\n`, err: '', timedOut: false, cancelled: false }
}

await asyncTest('installer: detect keeps 3.10+, runs the probed interpreter (not the launcher), reports found and too-old', async () => {
  const answers = {
    'C:\\Windows\\py.exe': probeAnswer({ version: [3, 12, 4], executable: 'C:\\Py312\\python.exe' }),
    'C:\\Py312\\python.exe': probeAnswer({ version: [3, 12, 4], executable: 'C:\\Py312\\python.exe' }),
    'C:\\old\\python.exe': probeAnswer({ version: [3, 9, 1], executable: 'C:\\old\\python.exe' }),
    'C:\\conda\\python.exe': probeAnswer({ version: [3, 13, 0], executable: 'C:\\conda\\python.exe',
      mcp: '0.6.1', mss: true, scripts: ['C:\\conda\\Scripts\\clawtouch-mcp.exe'] }),
  }
  const calls = []
  const runner = async (cmd, args, opts) => { calls.push({ cmd, args, cwd: opts.cwd }); return answers[cmd] ?? { code: 1, out: '', err: '' } }
  const files = new Set(['C:\\Windows\\py.exe', 'C:\\Py312\\python.exe', 'C:\\old\\python.exe', 'C:\\conda\\python.exe'])
  const out = await inst.detect({
    runner, platform: 'win32', home: 'C:\\h\\.dsh', mkdir: () => {},
    env: { SystemRoot: 'C:\\Windows', PATH: 'C:\\Py312;C:\\old;C:\\conda' },
    exists: (p) => files.has(p), list: () => [],
  })
  assert.deepEqual(out.pythons.map((p) => [p.id, p.command, p.version]),
    [[0, 'C:\\Py312\\python.exe', '3.12.4'], [1, 'C:\\conda\\python.exe', '3.13.0']])
  assert.deepEqual(out.found, [{ path: 'C:\\conda\\Scripts\\clawtouch-mcp.exe', version: '0.6.1',
    python: 'C:\\conda\\python.exe', screenshot: true }])
  assert.deepEqual(out.tooOld, [{ executable: 'C:\\old\\python.exe', version: '3.9.1' }])
  assert.equal(out.managed, null)
  assert.deepEqual(calls[0].args.slice(0, 5), ['-3', '-X', 'utf8', '-E', '-c'])
  for (const c of calls) assert.equal(c.cwd, 'C:\\h\\.dsh\\clawtouch-mcp\\probe')
})

await asyncTest('installer: an interpreter that fails or prints junk is skipped, not fatal', async () => {
  const runner = async (cmd) => (cmd.endsWith('a.exe') ? { code: 9009, out: 'Python was not found', err: '' }
    : { code: 0, out: 'not json', err: '' })
  const out = await inst.detect({ runner, platform: 'win32', home: 'C:\\h', mkdir: () => {},
    env: { PATH: 'C:\\x' }, exists: (p) => p.endsWith('python.exe'), list: () => [] })
  assert.deepEqual(out.pythons, [])
})

test('installer: credentials never reach the page, even split across output chunks', () => {
  assert.equal(inst.redact('via http://alice:s3cret@proxy:7890/ ...'), 'via http://***@proxy:7890/ ...')
  assert.equal(inst.redact('https://tok3n@repo.example/simple'), 'https://***@repo.example/simple')
  assert.equal(inst.redact('GET /simple?token=abc&x=1'), 'GET /simple?token=***&x=1')
  assert.equal(inst.redact('https://alice:p@ss@proxy.example/x'), 'https://***@proxy.example/x')
  assert.equal(inst.redact('GET /f?api_key=abc123&X-Amz-Signature=s1g&ok=1'), 'GET /f?api_key=***&X-Amz-Signature=***&ok=1')
  assert.equal(inst.redact('https://pypi.org/simple/'), 'https://pypi.org/simple/')
  const seen = []
  const split = inst.lineSplitter((l) => seen.push(l))
  split.push('Looking in https://alice:sec')
  split.push('ret@proxy.example/ now\nnext')
  split.end()
  assert.deepEqual(seen, ['Looking in https://***@proxy.example/ now', 'next'])
})

test('installer: the child environment drops what could redirect pip or Python, keeps proxies', () => {
  const env = inst.childEnv({ PIP_TARGET: '/x', PIP_EXTRA_INDEX_URL: 'https://evil', PYTHONPATH: '/p', PYTHONHOME: '/h',
    VIRTUAL_ENV: '/v', HTTPS_PROXY: 'http://127.0.0.1:7890', PATH: '/bin' }, 'linux')
  assert.deepEqual(Object.keys(env).sort(),
    ['HTTPS_PROXY', 'PATH', 'PIP_CONFIG_FILE', 'PIP_DISABLE_PIP_VERSION_CHECK', 'PIP_NO_INPUT'])
  // no pip configuration file at all: not the user's, the global one, or the venv's
  assert.equal(env.PIP_CONFIG_FILE, '/dev/null')
  assert.equal(inst.childEnv({}, 'win32').PIP_CONFIG_FILE, 'nul')
})

test('installer: versions compare as PEP 440 releases, pre-releases before them', () => {
  assert.equal(inst.compareVersions('0.6.10', '0.6.9'), 1)
  assert.equal(inst.compareVersions('0.6.1', '0.6.1'), 0)
  assert.equal(inst.compareVersions('0.6.0', '0.6.1'), -1)
  assert.equal(inst.compareVersions('0.6.1rc1', '0.6.1'), -1)
  assert.equal(inst.compareVersions('0.6.1.dev1', '0.6.1'), -1)
  assert.equal(inst.compareVersions('0.6.1.post1', '0.6.1'), 1)
  assert.equal(inst.compareVersions('3.10', '3.9.9'), 1)
})

/** A fake world for one install: which files exist, and what each command answers. */
function installWorld({ pip = [0], venv = 'none', verify = {}, marker = true, lockHeld = false, linked = false,
  pipTimedOut = [] } = {}) {
  const root = '/h/clawtouch-mcp'
  const files = new Set(marker ? [`${root}/.dsh-clawtouch`] : [])
  if (venv !== 'none') {
    files.add(`${root}/venv`)
    files.add(`${root}/venv/bin/python`)
    if (venv === 'healthy') files.add(`${root}/venv/pyvenv.cfg`)
  }
  if (lockHeld) files.add(`${root}/install.lock`)
  const calls = []
  const envs = []
  const pipAnswers = [...pip]
  const pipTimeouts = [...pipTimedOut]
  const runner = async (cmd, args, opts) => {
    calls.push([cmd, ...args].join(' '))
    envs.push(opts.env)
    opts.onLine?.('working')
    if (args.includes('venv') && args.includes('-m')) {
      files.add(`${root}/venv`); files.add(`${root}/venv/bin/python`); files.add(`${root}/venv/pyvenv.cfg`)
      return { code: 0, out: '', err: '' }
    }
    if (args.includes('pip')) {
      if (pipTimeouts.shift()) return { code: null, out: '', err: '', timedOut: true }
      const code = pipAnswers.shift() ?? 1
      if (code === 0) files.add(`${root}/venv/bin/clawtouch-mcp`)
      return { code, out: '', err: code ? 'Could not fetch URL' : '' }
    }
    // the probe, for health and for verification
    const installed = files.has(`${root}/venv/bin/clawtouch-mcp`)
    return probeAnswer({ version: [3, 12, 0], executable: `${root}/venv/bin/python`, invenv: true,
      mcp: installed ? '0.6.1' : null, mss: installed, ...verify })
  }
  const fs = {
    exists: (p) => files.has(p),
    mkdir: () => {},
    writeFile: (p) => files.add(p),
    lstat: () => ({ isSymbolicLink: () => linked }),
    open: (p) => { if (files.has(p)) throw Object.assign(new Error('exists'), { code: 'EEXIST' }); files.add(p); return 1 },
    write: () => {},
    close: () => {},
    stat: () => ({ mtimeMs: Date.now() }),
    unlink: (p) => files.delete(p),
    rm: () => {},
  }
  const installer = new inst.Installer({ runner, home: '/h', platform: 'linux', fs })
  return { installer, calls, files, envs }
}

const PY3 = { command: '/usr/bin/python3' }
const finished = (installer) => new Promise((resolve) => {
  const tick = () => (installer.running ? setTimeout(tick, 1) : resolve(installer.state()))
  tick()
})

await asyncTest('installer: a fresh install makes the venv, installs the pinned spec with its extras, isolated, and checks it', async () => {
  const { installer, calls, files } = installWorld({ marker: false })
  let ended = 0
  assert.equal(installer.start(PY3, () => { ended += 1 }), true)
  assert.equal(installer.start(PY3), false) // one at a time
  const state = await finished(installer)
  assert.deepEqual([state.phase, state.source, state.path], ['done', 'pypi', '/h/clawtouch-mcp/venv/bin/clawtouch-mcp'])
  assert.equal(ended, 1)
  assert.ok(files.has('/h/clawtouch-mcp/.dsh-clawtouch')) // the folder is marked as ours
  assert.ok(!files.has('/h/clawtouch-mcp/install.lock')) // and the lock released
  assert.ok(calls.some((c) => c === '/usr/bin/python3 -X utf8 -E -m venv /h/clawtouch-mcp/venv'))
  const pip = calls.find((c) => c.includes(' pip '))
  assert.ok(pip.includes('--isolated') && pip.includes('-i https://pypi.org/simple')
    && pip.endsWith(`clawtouch-mcp[screenshot,window]==${inst.MCP_VERSION}`), pip)
})

await asyncTest('installer: pip runs unbuffered, without a cache, with temp files in its own folder', async () => {
  const { installer, calls, envs } = installWorld()
  installer.start(PY3)
  await finished(installer)
  const i = calls.findIndex((c) => c.includes(' pip '))
  assert.ok(calls[i].includes('-u -m pip') && calls[i].includes('--no-cache-dir'), calls[i])
  assert.deepEqual([envs[i].TEMP, envs[i].TMP, envs[i].TMPDIR], Array(3).fill('/h/clawtouch-mcp/tmp'))
})

await asyncTest('installer: PyPI not answering in time gives way to the mirror', async () => {
  const { installer, calls } = installWorld({ pip: [0], pipTimedOut: [true] })
  installer.start(PY3)
  const state = await finished(installer)
  assert.deepEqual([state.phase, state.source], ['done', 'tuna'])
  assert.ok(state.lines.some((l) => l.includes('no answer from https://pypi.org/simple')))
  assert.equal(calls.filter((c) => c.includes(' pip ')).length, 2)
})

await asyncTest('installer: PyPI failing falls back to the mirror once', async () => {
  const { installer, calls } = installWorld({ pip: [1, 0] })
  installer.start(PY3)
  const state = await finished(installer)
  assert.deepEqual([state.phase, state.source], ['done', 'tuna'])
  assert.ok(calls.some((c) => c.includes(`-i ${inst.MIRROR}`)))
})

await asyncTest('installer: both failing is a failure with the output kept', async () => {
  const { installer } = installWorld({ pip: [1, 1] })
  installer.start(PY3)
  const state = await finished(installer)
  assert.deepEqual([state.phase, state.error], ['failed', 'pip'])
  assert.ok(state.lines.length > 0)
})

await asyncTest('installer: a healthy venv is reused; a half-built one is rebuilt with --clear', async () => {
  const reuse = installWorld({ venv: 'healthy' })
  reuse.installer.start(PY3)
  assert.equal((await finished(reuse.installer)).phase, 'done')
  assert.ok(!reuse.calls.some((c) => c.includes('-m venv')))
  const half = installWorld({ venv: 'half' }) // python there, no pyvenv.cfg
  half.installer.start(PY3)
  assert.equal((await finished(half.installer)).phase, 'done')
  assert.ok(half.calls.some((c) => c.includes('-m venv --clear')))
})

await asyncTest('installer: a venv folder not marked as ours, or a link, is never cleared', async () => {
  const foreign = installWorld({ venv: 'half', marker: false })
  foreign.installer.start(PY3)
  assert.equal((await finished(foreign.installer)).error, 'not-ours')
  const link = installWorld({ venv: 'half', linked: true })
  link.installer.start(PY3)
  assert.equal((await finished(link.installer)).error, 'not-ours')
  assert.ok(!link.calls.some((c) => c.includes('--clear')))
})

await asyncTest('installer: a healthy-looking venv that is a link is refused before pip runs', async () => {
  const { installer, calls } = installWorld({ venv: 'healthy', linked: true })
  installer.start(PY3)
  assert.equal((await finished(installer)).error, 'not-ours')
  assert.ok(!calls.some((c) => c.includes(' pip ')))
})

await asyncTest('installer: a lock file that cannot be written is not left behind', async () => {
  const w = installWorld()
  let unlinked = 0
  const fs = { ...w.installer.fs, write: () => { throw Object.assign(new Error('disk full'), { code: 'ENOSPC' }) },
    unlink: () => { unlinked += 1 } }
  const installer = new inst.Installer({ runner: async () => ({ code: 0, out: '', err: '' }), home: '/h', platform: 'linux', fs })
  installer.start(PY3)
  assert.equal((await finished(installer)).error, 'locked')
  assert.ok(unlinked >= 1)
})

await asyncTest('installer: the managed install is reported with its screenshot readiness', async () => {
  const runner = async () => probeAnswer({ version: [3, 12, 0], executable: '/h/clawtouch-mcp/venv/bin/python',
    invenv: true, mcp: '0.6.1', mss: false })
  const out = await inst.detect({ runner, platform: 'linux', home: '/h', mkdir: () => {}, env: { PATH: '' },
    exists: (p) => p === '/h/clawtouch-mcp/venv/bin/python' || p === '/h/clawtouch-mcp/venv/bin/clawtouch-mcp',
    list: () => [] })
  assert.deepEqual(out.managed, { path: '/h/clawtouch-mcp/venv/bin/clawtouch-mcp', version: '0.6.1', screenshot: false })
})

await asyncTest('installer: another install holding the folder lock is respected', async () => {
  const { installer, calls } = installWorld({ lockHeld: true })
  installer.start(PY3)
  assert.equal((await finished(installer)).error, 'locked')
  assert.deepEqual(calls, [])
})

await asyncTest('installer: missing screenshot support fails the check', async () => {
  const { installer } = installWorld({ verify: { mss: false } })
  installer.start(PY3)
  assert.equal((await finished(installer)).error, 'verify')
})

await asyncTest('installer: close() cancels a running job and refuses new ones', async () => {
  const runner = (cmd, args, opts) => new Promise((resolve) => {
    opts.signal?.addEventListener('abort', () => resolve({ code: null, out: '', err: '', cancelled: true }))
  })
  const w = installWorld()
  const installer = new inst.Installer({ runner, home: '/h', platform: 'linux', fs: { ...w.installer.fs } })
  installer.start(PY3)
  await new Promise((r) => setTimeout(r, 5))
  installer.close()
  const state = await finished(installer)
  assert.deepEqual([state.phase, state.error], ['failed', 'cancelled'])
  assert.equal(installer.start(PY3), false)
})

test('settings: switching to the clawtouch-mcp program drops a leftover `-m clawtouch_mcp`', () => {
  const current = { command: 'python', args: ['-m', 'clawtouch_mcp', '--port', 'COM7'] }
  const next = settingsMod.parseSave(SAVE({ command: 'C:\\h\\.dsh\\clawtouch-mcp\\venv\\Scripts\\clawtouch-mcp.exe' }))
  assert.deepEqual(settingsMod.withSettings(current, next, BUNDLE).args, ['--port', 'COM7'])
  const keep = settingsMod.withSettings(current, settingsMod.parseSave(SAVE({ command: 'python3' })), BUNDLE)
  assert.deepEqual(keep.args, ['-m', 'clawtouch_mcp', '--port', 'COM7'])
})

// ── the install routes ────────────────────────────────────────────────

function setupFixture({ detectResult } = {}) {
  const activity = new ActivityGate()
  let stopped = 0
  let onStop = async () => {}
  const mcp = { stop: async () => { stopped += 1; await onStop() } }
  const det = detectResult ?? { root: '/h/clawtouch-mcp', pythons: [{ id: 0, command: '/usr/bin/python3',
    executable: '/usr/bin/python3', version: '3.12.1', venv: true }], found: [], managed: null, tooOld: [] }
  let detects = 0
  const ins = { running: false, closed: false, started: null, state: () => null,
    start(py, onEnd) { if (this.closed) return false; this.started = py; this.onEnd = onEnd; this.running = true; return true } }
  const [setup, install] = routesMod.setupRoutes({ installer: ins, detect: async () => { detects += 1; return det },
    mcp, activity, required: '0.6.1' })
  const get = async (route) => (await route.fetch(new Request('http://dsh/api'))).json()
  const post = async (body) => {
    const res = await install.fetch(new Request('http://dsh/api', { method: 'POST', body: JSON.stringify(body) }))
    return { status: res.status, body: await res.json() }
  }
  return { setup, install, get, post, activity, ins, stopped: () => stopped, detects: () => detects,
    whileStopping: (fn) => { onStop = fn } }
}

await asyncTest('routes: setup lists Pythons without launch details, with a generation, and caches briefly', async () => {
  const f = setupFixture()
  const a = await f.get(f.setup)
  assert.deepEqual(a.pythons, [{ id: 0, executable: '/usr/bin/python3', version: '3.12.1', venv: true }])
  assert.equal(a.required, '0.6.1')
  // unique to this plugin instance: a reload starts a different sequence
  assert.match(a.generation, /^[0-9a-f]{8,}-1$/)
  const other = setupFixture()
  assert.notEqual(a.generation, (await other.get(other.setup)).generation)
  await f.get(f.setup)
  assert.equal(f.detects(), 1)
})

await asyncTest('routes: install takes a Python from the generation shown, closes the gate, stops the old server', async () => {
  const f = setupFixture()
  assert.equal((await f.post({ python: 0, generation: 'x-1' })).body.error, 'stale') // nothing shown yet
  const { generation } = await f.get(f.setup)
  assert.equal((await f.post({ python: 0, generation: `${generation}0` })).body.error, 'stale')
  const res = await f.post({ python: 0, generation })
  assert.equal(res.status, 202)
  assert.equal(f.ins.started.command, '/usr/bin/python3')
  assert.equal(f.stopped(), 1)
  await assert.rejects(f.activity.run(async () => 'pressed'), /installing clawtouch-mcp/)
  f.ins.onEnd()
  assert.equal(f.activity.idle, true)
  assert.deepEqual((await f.post({ python: 'C:\\evil.exe', generation })).body, { ok: false, error: 'invalid', field: 'python' })
})

await asyncTest('routes: a plugin disposed while the old server stops starts no install', async () => {
  const f = setupFixture()
  const { generation } = await f.get(f.setup)
  f.whileStopping(async () => { f.ins.closed = true; f.activity.closeForGood('reloading') })
  const res = await f.post({ python: 0, generation })
  assert.deepEqual([res.status, res.body.error], [409, 'reloading'])
  assert.equal(f.ins.started, null)
})

await asyncTest('routes: no install while a tool runs, nor a second one', async () => {
  const f = setupFixture()
  const { generation } = await f.get(f.setup)
  let release
  const tool = f.activity.run(() => new Promise((r) => { release = r }))
  assert.equal((await f.post({ python: 0, generation })).body.error, 'busy')
  release()
  await tool
  f.ins.running = true
  assert.equal((await f.post({ python: 0, generation })).body.error, 'installing')
})

await asyncTest('routes: a save during an install is refused as installing', async () => {
  const fiber = {}
  const editor = { entries: () => [{ fiber, options: { config: {} } }], edit: async () => {} }
  const route = routesMod.settingsRoute({ editor: () => editor, fiber, config: {}, activity: new ActivityGate(),
    installer: { running: true } })
  const res = await route.fetch(new Request('http://dsh/api', { method: 'POST', body: '{}' }))
  assert.deepEqual([res.status, (await res.json()).error], [409, 'installing'])
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
