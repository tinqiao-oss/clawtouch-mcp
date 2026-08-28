#!/usr/bin/env node
/**
 * Repo tool: exercise the plugin's pipeline from a terminal, without dsh.
 *
 * Setting this up has four independent things that can be wrong — the
 * device, the screen capture, the vision key, and the coordinate maths —
 * and inside a running agent they all present identically as "it clicked
 * the wrong place". Each mode here isolates one link:
 *
 *   node probe.js --windows              what can be targeted
 *   node probe.js --shot --window 微信   capture + markers, saved to disk
 *   node probe.js --move-test            device + coordinate maths, NO model
 *   node probe.js "the Send button"      the whole thing, locate only
 *   node probe.js "the Send button" --click
 *
 * `--move-test` is the useful one when nothing works yet: it drives the
 * real cursor to each marker's own screen position and reports how far
 * off it landed. That needs no API key, and a bad result there means the
 * problem is below the vision layer entirely.
 *
 * Not shipped in the npm package (`files` in package.json) — this is for
 * people working on the plugin, and for diagnosing an install.
 */
import { writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { McpStdioClient } from './lib/mcp-client.js'
import { Locator, describeResult } from './lib/locator.js'
import { calibrate, toScreenPoint } from './lib/calibrate.js'

const argv = process.argv.slice(2)
const flag = (n) => argv.includes(`--${n}`)
const opt = (n, d) => {
  const i = argv.indexOf(`--${n}`)
  return i >= 0 && argv[i + 1] ? argv[i + 1] : d
}
// Every option that takes a VALUE has to be listed here, or its value is
// read as part of the target description. `--screen` was added later and
// missed: `probe.js "the 7 key" --screen 7680x1440` asked the model to find
// "the 7 key 7680x1440", and the resulting nonsense answer looked exactly
// like a model bug for three runs.
const VALUE_OPTS = ['window', 'command', 'max-width', 'model', 'endpoint',
  'screen']
const positional = argv.filter((a, i) => !a.startsWith('--')
  && !(i > 0 && VALUE_OPTS.includes(argv[i - 1]?.replace(/^--/, ''))))

const config = {
  command: opt('command', 'clawtouch-mcp'),
  args: ['--allow-screenshot'],
  maxWidth: Number(opt('max-width', 1600)),
  imageFormat: flag('png') ? 'png' : 'jpeg',
  dryRun: !flag('click'),
  vision: {
    endpoint: opt('endpoint'),
    model: opt('model', 'qwen-vl-max'),
    apiKey: process.env.DASHSCOPE_API_KEY
      || process.env.CLAWTOUCH_VISION_API_KEY,
  },
}
if (flag('mock')) config.args.push('--mock')
// Multi-monitor: clawtouch-mcp clamps clicks to --screen, which defaults
// to the PRIMARY display, so a window on a second screen is unreachable
// until the whole virtual desktop is declared. Only helps for a display
// right of or below the primary — --screen has a size and no origin, so
// one placed left or above sits behind negative coordinates no WxH can
// reach, and the out-of-range hint says so rather than sending you here.
if (opt('screen')) config.args.push('--screen', opt('screen'))

const log = (level, msg) => {
  if (level === 'debug' && !flag('verbose')) return
  console.error(`[${level}] ${msg}`)
}

const mcp = new McpStdioClient({
  command: config.command, args: config.args, log,
})
const locator = new Locator({ mcp, config, log })

async function main() {
  if (flag('windows')) return showWindows()
  if (flag('shot')) return saveShot()
  if (flag('move-test')) return moveTest()
  if (!positional.length) {
    console.error('usage: probe.js [--windows|--shot|--move-test] '
      + '| "<what to find>" [--window <title>] [--click]')
    process.exitCode = 2
    return
  }
  const target = positional.join(' ')
  const window = opt('window')
  const result = flag('click')
    ? await locator.click({ target, window })
    : await locator.point({ target, window })
  console.log(describeResult(result, flag('click') ? 'clicked' : 'found'))
  console.log(`  model said        : ${JSON.stringify(result.answer.target.point)}`)
  console.log(`  markers reported  : ${JSON.stringify(result.answer.markers)}`)
  console.log(`  markers actual    : ${JSON.stringify(
    result.meta.markers.map((m) => m.center))}`)
  console.log(`  fit               : x=${result.fit.x.scale.toFixed(4)}`
    + ` (offset ${result.fit.x.offset.toFixed(1)}), `
    + `y=${result.fit.y.scale.toFixed(4)}`
    + ` (offset ${result.fit.y.offset.toFixed(1)})`)
  console.log(`  image point       : ${result.image.map((v) => v.toFixed(1))}`)
  console.log(`  screen point      : ${result.screen}`)
  console.log(`  capture           : ${JSON.stringify(result.meta.capture_rect)}`
    + ` -> ${result.meta.width}x${result.meta.height}`)
  console.log(`  timings           : capture ${result.timings.captureMs}ms, `
    + `vision ${result.timings.visionMs}ms`)
}

async function showWindows() {
  const wins = await locator.windows()
  for (const w of wins) {
    const [x1, y1, x2, y2] = w.rect
    console.log(`${w.foreground ? '*' : ' '} ${String(x2 - x1).padStart(5)}`
      + `x${String(y2 - y1).padEnd(5)} @(${x1},${y1})  ${w.title}`)
  }
}

async function saveShot() {
  const picked = await locator.resolveRegion({ window: opt('window') })
  const { meta, image } = await locator.capture({ region: picked.region })
  const ext = meta.format === 'png' ? 'png' : 'jpg'
  const out = join(tmpdir(), `clawtouch-probe.${ext}`)
  writeFileSync(out, Buffer.from(image.data, 'base64'))
  console.log(`captured ${picked.source}`)
  console.log(`  ${meta.width}x${meta.height} from `
    + `${JSON.stringify(meta.capture_rect)} `
    + `(scale ${meta.image_scale.map((v) => v.toFixed(4)).join(', ')})`)
  console.log(`  markers: ${JSON.stringify(meta.markers.map((m) => m.center))}`)
  console.log(`  saved  : ${out}`)
}

/**
 * Device + geometry check with no model in the loop.
 *
 * Each marker's screen position is known from the capture metadata alone.
 * Driving the cursor there and reading back where it landed tests the
 * serial link, the firmware, the OS cursor query, and the convergence
 * loop — everything under the vision layer. A few pixels of residual is
 * normal; tens of pixels means the coordinate spaces disagree and no
 * amount of prompt tuning will fix the clicks.
 */
async function moveTest() {
  const picked = await locator.resolveRegion({ window: opt('window') })
  const { meta } = await locator.capture({ region: picked.region })
  console.log(`capture: ${picked.source} -> ${meta.width}x${meta.height} `
    + `from ${JSON.stringify(meta.capture_rect)}`)

  // Sanity-check the calibration maths itself with a perfect model: if a
  // model reported the markers exactly, the fit must be the identity.
  const perfect = Object.fromEntries(
    meta.markers.map((m) => [m.id, m.center]))
  const fit = calibrate(meta.markers, perfect)
  console.log(`identity fit: x=${fit.x.scale.toFixed(6)} `
    + `y=${fit.y.scale.toFixed(6)} (both must be 1.000000)`)

  let worst = 0
  const problems = []
  for (const marker of meta.markers) {
    const screen = toScreenPoint(marker.center, meta.capture_rect, meta.image_scale)
    const res = await mcp.callTool('hid.move', { x: screen[0], y: screen[1] })
    const j = res.json ?? {}
    // Residual is measured against what we ASKED for, not against the
    // clamped target the server echoes back — otherwise a coordinate
    // clamped 120px away reports a 0px residual, which is how the first
    // version of this probe printed "OK" under two failed moves. It is
    // also computed for FAILED moves: where the cursor actually stopped
    // is the whole diagnosis.
    const dx = (j.x ?? 0) - screen[0]
    const dy = (j.y ?? 0) - screen[1]
    if (res.isError || j.error) {
      problems.push(`${marker.id}: ${j.clamped ? 'clamped' : 'no convergence'} `
        + `(off by ${dx}, ${dy})`)
      console.log(`  ${marker.id}: FAILED — asked (${screen[0]}, ${screen[1]}) `
        + `stopped at (${j.x}, ${j.y})  off by (${dx}, ${dy})`)
      if (j.hint) console.log(`        ${String(j.hint).slice(0, 200)}`)
      worst = Math.max(worst, Math.abs(dx), Math.abs(dy))
      continue
    }
    worst = Math.max(worst, Math.abs(dx), Math.abs(dy))
    const flags = []
    if (j.clamped) flags.push('CLAMPED')
    if (j.converged === false) flags.push('NOT CONVERGED')
    if (j.ok === false) flags.push('ok:false')
    if (flags.length) problems.push(`${marker.id}: ${flags.join(', ')}`)
    console.log(`  ${marker.id}: asked (${screen[0]}, ${screen[1]}) `
      + `landed (${j.x}, ${j.y})  off by (${dx}, ${dy})`
      + `${flags.length ? `  ${flags.join(' ')}` : ''}`)
    if (j.hint) console.log(`        ${j.hint}`)
  }
  if (problems.length || worst > 5) {
    console.log(`\nPROBLEM — worst residual ${worst}px`
      + `${problems.length ? `; ${problems.join('; ')}` : ''}.`
      + '\nFix this before blaming the vision model.')
    process.exitCode = 1
    return
  }
  console.log(`\nOK — worst residual ${worst}px. `
    + 'The device and the geometry agree.')
}

main()
  .catch((err) => {
    console.error(`\n${err.message}`)
    process.exitCode = 1
  })
  .finally(() => mcp.stop())
