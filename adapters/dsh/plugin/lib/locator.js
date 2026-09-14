/**
 * The five steps, in one place.
 *
 * Everything the agent-facing tools do reduces to this sequence:
 *
 *   1. pick a rectangle  — the target window, not the whole desktop
 *   2. capture it, bounded to a width a vision model can actually use
 *   3. stamp two markers whose image positions we know exactly
 *   4. one vision call: both markers AND the target, same rendering
 *   5. fit model→image from the markers, map the target, then click
 *
 * Skipping any one of them is what makes visual clicking "sometimes
 * work": a full ultrawide capture loses the detail (2), no markers leaves
 * the model's internal resize unmeasured (3,5), and a separate
 * calibration call measures a *different* rendering than the one the
 * target was found in (4).
 *
 * The agent never sees an image and never sees a coordinate. It says what
 * it wants clicked; this returns what happened, in words.
 */
import { locate, VisionError } from './vision.js'
import { calibrate, resolvePoint, CalibrationError } from './calibrate.js'

export class LocateError extends Error {}

/**
 * The vision model looked and did not see it.
 *
 * Distinct from LocateError because it is not a failure of the machinery:
 * for `computer_find` it is the answer, and only a caller about to CLICK
 * has to treat it as an error. Collapsing the two would make "the button
 * is not on screen" indistinguishable from "the device is unplugged".
 */
export class TargetNotFound extends LocateError {}

/** Widest image we will hand a vision model unless configured otherwise.
 *  Measured on this project's own runs: the same desktop at 5120px wide
 *  located 0 of 6 targets; cropped and capped to 1920 it located 6 of 6. */
const DEFAULT_MAX_WIDTH = 1600

/**
 * How much of a window must be on top before we are willing to look at it.
 *
 * Capturing a window's rectangle captures whatever is IN FRONT of that
 * rectangle. Point a vision model at a covered window and it describes
 * the app sitting on top — fluently, coherently and about the wrong
 * application. That answer is indistinguishable from a right one, so the
 * only safe move is to refuse before the model is ever asked.
 *
 * Not 1.0: a tooltip or a notification toast clipping a corner is not a
 * reason to stop working. Below this, though, the capture is mostly
 * something else.
 */
const MIN_VISIBLE_FRACTION = 0.6

/** Most targets one `computer_click_sequence` may carry.
 *
 *  `hid.batch` executes at most 10 ops, and the point of the cap is not
 *  the wire limit: every extra click is another chance for the screen to
 *  have changed since the one capture they were all located in. Eight
 *  leaves headroom under the batch limit and stays short enough that the
 *  "nothing moves in between" assumption is checkable by eye. */
const MAX_SEQUENCE = 8

/** How long to let the window manager finish raising before looking again. */
const RAISE_SETTLE_MS = 450

export class Locator {
  /**
   * @param {object} opts
   * @param {import('./mcp-client.js').McpStdioClient} opts.mcp
   * @param {object} opts.config
   * @param {(level: string, msg: string) => void} [opts.log]
   */
  constructor(opts) {
    this.mcp = opts.mcp
    this.config = opts.config
    this.log = opts.log ?? (() => {})
  }

  /**
   * What the server reports about itself and its input device, or null
   * when that could not be read.
   *
   * Asked once: everything read from it here comes from startup flags
   * (`--screen`, `--mock`), fixed for the life of the process, and one extra
   * round trip per click is not free. The pending request is what gets
   * cached, so two tools asking at once share one question.
   *
   * Only an answer is remembered — an object carrying the `info` block the
   * server always sends. An error, a reply with no JSON, or JSON of another
   * shape is not an answer: remembering it would decide "no bounds, not a
   * mock" for the rest of the session on the strength of one failed read.
   * It reads as null now and is asked again next time.
   */
  deviceInfo() {
    if (!this._info) {
      const pending = this.mcp.callTool('device.info', {}).then(
        (res) => {
          if (res.isError || !isAnswer(res.json)) {
            if (this._info === pending) this._info = undefined
            return null
          }
          return res.json
        },
        (err) => {
          if (this._info === pending) this._info = undefined
          throw err
        })
      this._info = pending
    }
    return this._info
  }

  /**
   * The screen rectangle `hid.click` can actually address, or null when
   * the server was given no bounds at all (then it clamps nothing) — or
   * when its answer could not be read, which is not remembered.
   */
  async screenBounds() {
    const screen = (await this.deviceInfo())?.screen
    return (screen && screen.width > 0 && screen.height > 0
      && screen.source !== 'unset')
      ? { width: screen.width, height: screen.height, source: screen.source }
      : null
  }

  /**
   * True when the server runs with `--mock`: there is no device, every
   * action answers `ok`, and nothing is pressed. `mock: true` is what
   * someone without a board reaches for first, and relaying those answers
   * as "clicked" is the report this plugin can least afford to get wrong.
   *
   * Two sources, either one enough: the flag this plugin itself started the
   * server with (known without asking), and the server's own answer (for a
   * `--mock` that arrived through a wrapper command instead).
   *
   * When neither can say, this THROWS rather than guessing "a device":
   * whether the input about to go out would press anything is then unknown,
   * and every caller asks this before sending — so the refusal lands before
   * any input, and once anything has been sent the answer is already cached
   * and cannot fail afterwards.
   */
  async simulated() {
    if (Array.isArray(this.config.args) && this.config.args.includes('--mock')) {
      return true
    }
    const answer = await this.deviceInfo()
    if (!answer) {
      throw new LocateError(
        'clawtouch-mcp did not say what it is driving (its device.info reply '
        + 'could not be read), so whether this input would reach a device or '
        + 'a --mock stand-in is unknown; nothing was sent. Check that the '
        + 'server started cleanly, then try again.')
    }
    return answer.info.mock === true
  }

  /** Visible top-level windows, for the agent to choose from. */
  async windows() {
    const res = await this.mcp.callTool('screen.windows', {})
    if (res.isError || res.json?.error) {
      throw new LocateError(res.json?.error ?? res.text)
    }
    return res.json?.windows ?? []
  }

  /**
   * Resolve the rectangle to capture.
   *
   * Order matters: an explicit region wins, then a named window, then the
   * foreground window. Falling all the way through to a full-screen
   * capture is deliberate but last — it is the case most likely to be too
   * wide to locate anything in, so it is what you get only when nothing
   * better was available.
   */
  async resolveRegion({ window, region }) {
    if (Array.isArray(region) && region.length === 4) {
      return { region, source: 'explicit region' }
    }
    let wins
    try {
      wins = await this.windows()
    } catch (err) {
      // A NAMED window cannot be honoured by widening. Capturing the whole
      // desktop answers a different question than the one asked, and the
      // model then locates in UI the caller never mentioned — a confident
      // click somewhere else. Only the unnamed case has a fallback that
      // still means what the caller wanted.
      //
      // This became reachable in a new way when the server stopped
      // reporting an unreadable window list as an empty desktop: before
      // that, a macOS NULL listing arrived here as "no visible window
      // matching X" and this path refused. It has to keep refusing.
      if (window) {
        throw new LocateError(
          `"${window}" was asked for, but the window list could not be read `
          + '— so there is no way to find it, and no way to know it is not '
          + `there; nothing was captured: ${err.message}`)
      }
      this.log('warn', `window list unavailable (${err.message}); `
        + 'falling back to a full-screen capture')
      return { region: undefined, source: 'full screen' }
    }
    if (window) {
      const res = await this.mcp.callTool('screen.windows', { title: window })
      const match = res.json?.window
      if (!match) {
        // A lookup that FAILED is not an answer of "not there", and the
        // two are told apart by `available`: the server sends that list
        // only on a genuine miss. Without it this reported an unreadable
        // listing as a measured absence — and, because it fell back to
        // the FIRST listing's titles, produced answers of the form
        //   no visible window matching "Calc". Visible windows: "Calc"
        // while throwing away the server's actual explanation. The same
        // rule is already applied to the post-raise re-read below.
        const available = res.json?.available
        if (!Array.isArray(available)) {
          throw new LocateError(
            `could not look for a window matching "${window}" — the window `
            + 'listing failed, so whether it is there is unknown: '
            + `${res.json?.error ?? res.text.slice(0, 200)}`)
        }
        const titles = available.slice(0, 20)
        throw new LocateError(
          `no visible window matching "${window}". Visible windows: `
          + `${titles.map((t) => JSON.stringify(t)).join(', ') || '(none)'}`)
      }
      const ready = await this.ensureReachable(match)
      return {
        region: ready.rect,
        source: `window "${ready.title}"${unmeasuredNote(ready)}`,
      }
    }
    // `foreground` can be absent from every window: macOS answers it from
    // a real query now, and a query that could not be made flags nothing
    // rather than guessing. Falling back to the first window is still the
    // most useful thing to do — but it must not be REPORTED as the
    // foreground one, or the plugin re-tells exactly the guess the server
    // stopped making, in a string the agent has no way to check.
    const flagged = wins.find((w) => w.foreground)
    const front = flagged ?? wins[0]
    if (!front) return { region: undefined, source: 'full screen' }
    const ready = await this.ensureReachable(front)
    const what = flagged
      ? `foreground window "${ready.title}"`
      : `first listed window "${ready.title}" (nothing reported itself as `
        + 'foreground)'
    return {
      region: ready.rect,
      source: `${what}${unmeasuredNote(ready)}`,
    }
  }

  /**
   * Bring the window forward if it needs it, the way a person would.
   *
   * A window that is behind another one, or merely not focused, cannot be
   * clicked reliably: the capture would be of whatever is in front, and
   * some applications swallow the first click as an activation. So it is
   * raised first — by CLICKING it, through the same physical mouse, not
   * by a focus-stealing API. `screen.windows` supplies the point: one the
   * application itself reports as a drag area, picked from the right end
   * of the caption because a drag-area answer is not by itself a promise
   * that a click does nothing (Chrome's "new tab" button gives that answer
   * too). Which is why the window is re-read afterwards rather than
   * trusted: if the click did something else, that shows up here.
   *
   * Refuses rather than raising when a modal dialog has disabled the
   * window (raising it changes nothing) or when no safe point exists (a
   * full-screen app has no caption to grab).
   */
  async ensureReachable(win) {
    if (win.enabled === false) {
      assertOnTop(win)   // throws with the modal-dialog explanation
      return win
    }
    const visible = typeof win.visible_fraction === 'number'
      ? win.visible_fraction : 1
    if (win.foreground && visible >= MIN_VISIBLE_FRACTION) return win

    const point = win.raise_point
    if (this.config.autoRaise === false || !Array.isArray(point)) {
      assertOnTop(win)
      // Visible enough to work with, just not focused: carry on rather
      // than refusing over a distinction that may not matter.
      return win
    }
    // A dry run promises that nothing is pressed, and raising IS a press —
    // a real click on the title bar, which in a browser can open a tab.
    if (this.config.dryRun) return this.asIs(win, 'dryRun presses nothing')

    const bounds = await this.screenBounds()
    // A --mock server "clicks" by logging it. The re-read afterwards would
    // then say the raise did not take, which is true and misleading as to
    // why.
    if (await this.simulated()) {
      return this.asIs(win, 'clawtouch-mcp is running with --mock (no device)')
    }
    const [rx, ry] = point
    if (bounds && (rx < 0 || ry < 0
        || rx >= bounds.width || ry >= bounds.height)) {
      // The point exists, but it is outside the screen this session was
      // told about — a second monitor when only the primary was declared,
      // where a click would be clamped to somewhere else entirely. Worth
      // saying out loud: not raising looks exactly like a window that
      // needed no raise, and the cause is not guessable from the outcome.
      this.log('warn',
        `"${win.title}" has a raise point at (${rx}, ${ry}), outside the `
        + `declared screen (${bounds.width}x${bounds.height}). `
        + `${outOfBoundsFix(rx, ry, bounds)} Carrying on without raising.`)
      assertOnTop(win)
      return win
    }

    this.log('info', `raising "${win.title}" by clicking (${rx}, ${ry})`)
    const res = await this.mcp.callTool('hid.click', { x: rx, y: ry })
    if (res.isError || res.json?.ok !== true || res.json?.clicked !== true) {
      throw new LocateError(
        `"${win.title}" needed to be brought to the front, and the click `
        + `to do it was not confirmed: ${res.json?.hint ?? res.text.slice(0, 200)}`)
    }
    await new Promise((r) => setTimeout(r, RAISE_SETTLE_MS))

    const after = await this.mcp.callTool(
      'screen.windows', { title: win.title })
    // Re-checked, not assumed: the click may have raised something else,
    // or the window may have been closed while we were reaching for it.
    // A re-read that did not come back is not evidence that the raise
    // worked, so it must not quietly fall back to the pre-click window —
    // that would be the assumption this re-read exists to replace.
    if (after.isError || !after.json?.window) {
      throw new LocateError(
        `"${win.title}" was clicked to bring it to the front, but it could `
        + 'not be read back afterwards, so whether it actually came forward '
        + `is unknown: ${after.json?.hint ?? after.text.slice(0, 200)}`)
    }
    const fresh = after.json.window
    // The re-read asks by TITLE, which is mutable and not unique — and
    // the server falls back to a substring match. If the target renamed
    // itself during those 450ms (a player changing track, an editor
    // changing file) another window can answer to the old title, and
    // everything after this would be aimed at that one instead. So the
    // identity is checked rather than assumed.
    if (!sameWindow(win, fresh)) {
      throw new LocateError(
        `"${win.title}" was clicked to bring it forward, but reading it back `
        + `returned a different window (pid ${fresh.pid}, rect `
        + `${JSON.stringify(fresh.rect)}, not pid ${win.pid} rect `
        + `${JSON.stringify(win.rect)}) — that title now matches more than `
        + 'one window, so which one is in front cannot be established. List '
        + 'the windows again and name it more precisely.')
    }
    // A2: we clicked it ON PURPOSE. A re-read that says it is still not in
    // front is a failed action, not a detail — and the next click may be
    // swallowed as activation instead of doing what it was aimed at.
    if (!raiseTookEffect(fresh)) {
      throw new LocateError(
        `"${win.title}" was clicked to bring it to the front, and reading it `
        + 'back says it is still not the foreground window — the raise did '
        + 'not take. A click aimed at it now may be swallowed as activation '
        + 'instead of doing what it was aimed at. Something is holding focus '
        + '(a modal elsewhere, or the OS refusing the change); deal with '
        + 'that first.')
    }
    assertOnTop(fresh)
    return fresh
  }

  /**
   * Work with a window as it stands, because this session cannot click to
   * raise it: fine when enough of it is showing, refused when it is covered
   * — with the reason no raise was attempted, since the usual remedy
   * ("bring it to the front") is exactly what this session cannot do.
   */
  asIs(win, why) {
    this.log('info', `${why}: not clicking to bring "${win.title}" forward`)
    try {
      assertOnTop(win)
    } catch (err) {
      if (err instanceof LocateError) {
        throw new LocateError(`${err.message} (No click was made to raise it: ${why}.)`)
      }
      throw err
    }
    return win
  }

  /** Capture one calibrated screenshot: markers stamped, width bounded. */
  async capture({ region }) {
    const args = {
      markers: true,
      max_width: this.config.maxWidth || DEFAULT_MAX_WIDTH,
      format: this.config.imageFormat || 'jpeg',
    }
    if (region) args.region = region
    const res = await this.mcp.callTool('hid.screenshot', args)
    if (res.isError) {
      throw new LocateError(`screenshot failed: ${res.text}`)
    }
    const meta = res.json
    const image = res.images[0]
    if (!image?.data) {
      throw new LocateError(
        `screenshot returned no image data (${res.text.slice(0, 200)})`)
    }
    if (!meta?.markers?.length || !meta.capture_rect || !meta.image_scale) {
      throw new LocateError(
        'screenshot metadata is missing markers/capture_rect/image_scale — '
        + 'clawtouch-mcp is older than 0.5.0; upgrade it')
    }
    return { meta, image }
  }

  /**
   * Where is `target` on screen? Does not click.
   * @returns {Promise<{screen: [number, number], image: [number, number],
   *                    meta: object, answer: object, fit: object,
   *                    source: string, timings: object}>}
   */
  async point({ target, window, region, signal }) {
    const many = await this.pointMany({
      targets: [target], window, region, signal,
    })
    return { ...many.results[0], meta: many.meta, answer: many.answer,
      fit: many.fit, source: many.source, timings: many.timings }
  }

  /**
   * Locate several targets from ONE capture and ONE vision call.
   *
   * Only sound while the screen does not change between them, which is
   * why this is a separate entry point rather than an optimisation the
   * caller gets for free: clicking a calculator key leaves the keypad
   * where it was, clicking a conversation replaces the whole pane, and
   * only the caller knows which kind of thing it is asking for.
   */
  async pointMany({ targets, window, region, signal }) {
    const wanted = (targets ?? [])
      .map((t) => String(t ?? '').trim())
      .filter(Boolean)
    if (!wanted.length) throw new LocateError('no target was described')
    if (wanted.length > MAX_SEQUENCE) {
      throw new LocateError(
        `${wanted.length} targets is more than the ${MAX_SEQUENCE} this can `
        + 'locate from a single look; split it up, and re-look in between '
        + 'if the screen changes')
    }
    const t0 = Date.now()
    const picked = await this.resolveRegion({ window, region })
    const { meta, image } = await this.capture({ region: picked.region })
    const tShot = Date.now()

    let answer
    try {
      answer = await locate({
        imageBase64: image.data,
        mimeType: image.mimeType || meta.mime_type || 'image/jpeg',
        targets: wanted,
        markerHint: meta.marker_hint ?? '',
        config: this.config.vision ?? {},
        signal,
      })
    } catch (err) {
      if (err instanceof VisionError) throw new LocateError(err.message)
      throw err
    }
    const tVision = Date.now()
    if (answer.repaired) {
      // Logged, not hidden: the repair's justification is a measured rate,
      // and a rate stays true only while someone can keep counting it.
      this.log('info', 'vision reply left "markers" unclosed; repaired the '
        + 'one missing brace (see parseAnswer)')
    }

    let fit
    try {
      fit = calibrate(meta.markers, answer.markers,
        { width: meta.width, height: meta.height })
    } catch (err) {
      if (err instanceof CalibrationError) throw new LocateError(err.message)
      throw err
    }

    const results = []
    const missing = []
    wanted.forEach((description, i) => {
      const found = answer.targets[i]
      if (!found?.found || !found.point) {
        missing.push(description)
        return
      }
      try {
        results.push({
          ...resolvePoint(fit, found.point, meta),
          target: description,
          label: found.label,
          confidence: found.confidence,
        })
      } catch (err) {
        if (err instanceof CalibrationError) {
          missing.push(`${description} (${err.message})`)
          return
        }
        throw err
      }
    })

    // All-or-nothing on purpose. A sequence is a sequence: clicking three
    // of four keys does not leave the application in a state anyone asked
    // for, and it is far harder to recover from than not having started.
    if (missing.length) {
      throw new TargetNotFound(
        `not found in the ${picked.source} capture: `
        + `${missing.map((m) => JSON.stringify(m)).join(', ')}`
        + (results.length
          ? ` (the other ${results.length} were found, but nothing was `
            + 'clicked — a half-done sequence is worse than none)'
          : ''))
    }

    return {
      results,
      meta,
      answer,
      fit,
      source: picked.source,
      timings: {
        captureMs: tShot - t0,
        visionMs: tVision - tShot,
        totalMs: tVision - t0,
      },
    }
  }

  /** Locate, then actually click. */
  async click({ target, window, region, button, double, moveMs, signal }) {
    const located = await this.point({ target, window, region, signal })
    const [x, y] = located.screen
    if (this.config.dryRun) {
      return { ...located, clicked: false, dryRun: true }
    }
    // Refuse BEFORE sending. clawtouch-mcp clamps an out-of-range
    // coordinate into the addressable screen and then genuinely clicks
    // there — by design, so an off-by-one at the edge still works. Here
    // that is never right: the point came from looking at a specific
    // window, so being outside the addressable area means the whole
    // window is (a second monitor the server was not told about). Noticing
    // afterwards, as this used to, means the wrong click already happened.
    const bounds = await this.screenBounds()
    if (bounds && (x < 0 || y < 0 || x >= bounds.width || y >= bounds.height)) {
      const why = bounds.source === 'detected'
        ? 'the server auto-detected the PRIMARY monitor only'
        : 'the server was started with those bounds'
      throw new LocateError(
        `(${x}, ${y}) is outside the ${bounds.width}x${bounds.height} screen `
        + 'clawtouch-mcp can address, so the click would land somewhere '
        + 'else entirely; nothing was sent. '
        + outOfBoundsFix(x, y, bounds, why))
    }
    // Settled BEFORE sending: a question asked after the click went out
    // could fail and report a failure for a click that happened.
    const simulated = await this.simulated()
    const res = await this.mcp.callTool('hid.click', {
      x, y,
      button: button || 'left',
      double: Boolean(double),
      ...(moveMs ? { move_ms: moveMs } : {}),
    })
    if (res.isError) {
      throw new LocateError(`located (${x}, ${y}) but the click failed: `
        + res.text.slice(0, 300))
    }
    // Demand POSITIVE confirmation, not merely the absence of an error.
    // `hid.click` answers with `ok` and `clicked`; a reply that carries
    // neither is a reply we cannot read, and reporting a click we cannot
    // confirm is the failure mode this project pays most dearly for.
    if (!res.json || res.json.ok !== true || res.json.clicked !== true) {
      throw new LocateError(
        `located (${x}, ${y}) but the click was not confirmed: `
        + `${res.json?.hint ?? (res.text.slice(0, 300) || 'no result body')}`)
    }
    // A clamped click DID happen — somewhere else. Kept as a second line
    // of defence behind the pre-flight check above, for a server whose
    // bounds we could not read.
    if (res.json.clamped) {
      throw new LocateError(
        `the click was clamped away from (${x}, ${y}): ${res.json.hint}`)
    }
    // A --mock server answers `clicked: true` for a click that happened
    // nowhere. Sent anyway — exercising the path is what --mock is for —
    // but not reported as a click.
    if (simulated) {
      return { ...located, clicked: false, simulated: true, click: res.json }
    }
    return { ...located, clicked: true, click: res.json }
  }

  /**
   * Locate several targets in one look, then click them in order.
   *
   * The clicks go out as one `hid.batch`, which paces discrete clicks
   * apart by default — back-to-back clicks with no gap get coalesced or
   * dropped by the OS while every one of them still reports success.
   */
  async clickSequence({ targets, window, region, button, signal }) {
    const located = await this.pointMany({ targets, window, region, signal })
    if (this.config.dryRun) {
      return { ...located, clicked: false, dryRun: true }
    }
    const bounds = await this.screenBounds()
    for (const r of located.results) {
      const [x, y] = r.screen
      if (bounds && (x < 0 || y < 0 || x >= bounds.width || y >= bounds.height)) {
        throw new LocateError(
          `${JSON.stringify(r.target)} maps to (${x}, ${y}), outside the `
          + `${bounds.width}x${bounds.height} screen clawtouch-mcp can `
          + `address; nothing was sent. ${outOfBoundsFix(x, y, bounds)}`)
      }
    }
    const simulated = await this.simulated()
    const res = await this.mcp.callTool('hid.batch', {
      ops: located.results.map((r) => ({
        type: 'click',
        x: r.screen[0],
        y: r.screen[1],
        button: button || 'left',
      })),
    })
    if (res.isError) {
      throw new LocateError(`the click sequence failed: ${res.text.slice(0, 400)}`)
    }
    // Demand positive confirmation for the batch AND for every op in it.
    // `hid.batch` reports a per-op failure inside an otherwise fine
    // envelope, and a sequence that clicked three of four is exactly the
    // outcome the caller must not mistake for success.
    const ops = res.json?.results ?? res.json?.ops ?? []
    const failed = ops
      .map((op, i) => ({ op, i }))
      .filter(({ op }) => op && op.ok === false)
    if (!res.json || res.json.ok !== true || failed.length) {
      const which = failed
        .map(({ i }) => JSON.stringify(located.results[i]?.target ?? i + 1))
        .join(', ')
      throw new LocateError(
        `the click sequence was not confirmed${which ? ` (failed at ${which})` : ''}: `
        + `${res.text.slice(0, 300) || 'no result body'}`)
    }
    if (simulated) {
      return { ...located, clicked: false, simulated: true, batch: res.json }
    }
    return { ...located, clicked: true, batch: res.json }
  }
}

/** A `device.info` reply that says something: an object with an `info` block. */
function isAnswer(json) {
  return Boolean(json) && typeof json === 'object' && !Array.isArray(json)
    && Boolean(json.info) && typeof json.info === 'object' && !Array.isArray(json.info)
}

/**
 * Appended to every result that pressed nothing, so a "would click" is
 * never read as a click that happened. Exported for the tools that call
 * the server directly (type, key, scroll).
 */
export const SIMULATED_NOTE = 'nothing was pressed: clawtouch-mcp is running '
  + 'with --mock, which has no device attached'
export const DRY_RUN_NOTE = 'nothing was pressed: dryRun is on'

/** The reason a result pressed nothing, or '' when it pressed for real. */
export function notPressedNote(result) {
  if (result?.simulated) return SIMULATED_NOTE
  if (result?.dryRun) return DRY_RUN_NOTE
  return ''
}

/**
 * Refuse a window that is mostly hidden behind another one.
 *
 * The message names the remedy the agent can carry out with the tools it
 * already has: click the window — in the taskbar, or any sliver of it
 * still showing — which brings it to the front the way a person would,
 * through the same physical mouse. No focus-stealing API involved.
 */
/**
 * Where occlusion could not be measured, say so on the answer itself.
 *
 * `assertOnTop` lets an unmeasured window through — refusing would make
 * the whole plugin unusable on the platforms that cannot measure it — but
 * silence there reads as "looked, and it is on top". The guard not having
 * run is precisely the thing worth telling the agent about.
 */
export function unmeasuredNote(win) {
  // Each is named separately, because they do not always go missing
  // together: macOS reports no occlusion and no input state, a minimised
  // window on Windows has its input state but no occlusion figure, and a
  // rectangle too small to sample has the input state but no occlusion
  // either. Reporting only one would leave the others silently unmeasured.
  //
  // `foreground` is here for the case where the OS could not be asked at
  // all — normally it IS measured and present, which is why its absence
  // is worth saying out loud rather than papering over with false.
  const missing = []
  if (typeof win.enabled !== 'boolean') missing.push('input state')
  if (typeof win.visible_fraction !== 'number') missing.push('occlusion')
  if (typeof win.foreground !== 'boolean') missing.push('which is frontmost')
  return missing.length ? ` (${missing.join(' and ')} unmeasured here)` : ''
}

/**
 * What to actually do about a point outside the addressable screen.
 *
 * A point can be out of range in two independent ways, and only one of
 * them is fixed by widening `--screen`. Past the right or bottom edge,
 * that is the whole answer. Below zero it is no answer at all: `--screen`
 * carries a size and no origin, so the addressable area always starts at
 * (0, 0) and no `WxH` admits a negative coordinate. Measured on macOS
 * with the second display moved to origin (-1920, 0): `--screen
 * 3432x1200` — the size of the whole virtual desktop — still addresses
 * only [0,3432)x[0,1200), and every point on that display clamps to
 * x=0. Telling that caller to widen `--screen` sends them to redo what
 * they already did, and the second identical failure reads as a broken
 * tool rather than an unreachable point.
 *
 * A point can be BOTH (negative x, past the bottom edge), so the two
 * halves are emitted independently rather than as an either/or — and
 * the negative half also has to mention widening, because moving the
 * display right of the primary puts it past the old bounds instead.
 *
 * Deliberately not offered as a remedy: `hid.click`/`hid.move` with
 * `relative: true` skip clamping entirely and could physically reach
 * that display. It is not a fix for anyone here — a relative delta
 * cannot be aimed at a point this layer located in absolute space.
 */
export function outOfBoundsFix(x, y, bounds, why) {
  const parts = []
  if (x < 0 || y < 0) {
    parts.push('A negative coordinate is out of range whatever --screen '
      + 'says: the flag carries a size and no origin, so the addressable '
      + 'area always starts at (0, 0). If this is a display placed left '
      + 'of or above the primary one, move it right of or below in the OS '
      + 'display settings — and give --screen a size that includes where '
      + 'it lands.')
  }
  // Without bounds we cannot tell a far-edge overflow from anything
  // else, so the generic advice is a fallback for having nothing
  // else to say — never an addition to the negative half, which
  // would re-attach the advice that cannot work.
  const pastEdge = bounds
    ? (x >= bounds.width || y >= bounds.height)
    : parts.length === 0
  if (pastEdge) {
    parts.push(why
      ? `Because ${why}, pass --screen WxH covering the whole virtual `
        + 'desktop to reach it.'
      : 'Pass --screen WxH covering the whole virtual desktop to reach it.')
  }
  return parts.join(' ')
}

/**
 * One agent-facing line for a window in `computer_windows`' answer.
 *
 * Lives here, not in index.js, for a mundane but load-bearing reason:
 * index.js imports `@deepseek-ai/dsh-tools`, which the published plugin
 * does not depend on and CI does not install, so anything test.js needs
 * to reach has to sit below that import. It is also the right side of the
 * line conceptually — this is the same "say which answers you do not
 * have" rule as `unmeasuredNote`, applied to the tool's OUTPUT shape
 * (`visible_percent` / `accepts_input`) rather than the raw window dict
 * (`visible_fraction` / `enabled`).
 *
 * The star can only ever say YES. Everything it cannot say — including a
 * `foreground` that is missing rather than false — has to be in the note,
 * or "not in front" and "nobody asked" render identically.
 */
/**
 * Is the window that came back the SAME one we were working with?
 *
 * One place for this question, because it kept being answered partly.
 * The re-read asks by TITLE — mutable, and not unique — so the reply can
 * be a different window that happens to answer to it. `pid` alone was
 * the first answer and it is not enough: two windows of the SAME
 * application share a pid, so a sibling passed while the screenshot and
 * every click after it went somewhere else.
 *
 * `rect` completes it. Raising a window does not move it, so a rectangle
 * that changed is a different window (or one that moved under us, which
 * is equally disqualifying for a point computed against the old one).
 * Both are compared only when both sides carry them — an absent field is
 * not evidence either way, here as everywhere else in this codebase.
 */
export function sameWindow(before, after) {
  if (typeof before.pid === 'number' && typeof after.pid === 'number'
      && before.pid !== after.pid) {
    return false
  }
  const a = before.rect
  const b = after.rect
  if (Array.isArray(a) && Array.isArray(b)
      && a.length === 4 && b.length === 4) {
    return a.every((v, i) => v === b[i])
  }
  return true
}

/**
 * Did the raise we performed actually take?
 *
 * The other half of the same mistake: we had the answer and did not read
 * it. We clicked the window ON PURPOSE to bring it forward, so a re-read
 * that says `foreground: false` is a failed action and has to be
 * reported — the README promises exactly that. Some applications swallow
 * the first click as activation, so "clicked, still not in front" is the
 * shape of a target click that will do nothing.
 *
 * `false` only. An ABSENT `foreground` means the platform could not be
 * asked, and refusing on that would ground the plugin wherever the
 * question is unanswerable — the same reason the occlusion guard lets an
 * unmeasured window through. Unknown is carried in the note, not in a
 * refusal.
 */
export function raiseTookEffect(fresh) {
  return fresh.foreground !== false
}

export function renderWindowLine(w) {
  const unmeasured = []
  if (typeof w.accepts_input !== 'boolean') unmeasured.push('input state')
  if (typeof w.visible_percent !== 'number') unmeasured.push('occlusion')
  if (typeof w.foreground !== 'boolean') unmeasured.push('which is frontmost')
  const listed = unmeasured.length > 2
    ? `${unmeasured.slice(0, -1).join(', ')} and `
      + `${unmeasured[unmeasured.length - 1]}`
    : unmeasured.join(' and ')
  return `${w.foreground ? '* ' : '  '}${w.title}  (${w.width}x${w.height})`
    + (w.accepts_input === false
      ? '  — NOT accepting input, a modal dialog is over it'
      : '')
    + (typeof w.visible_percent === 'number' && w.visible_percent < 100
      ? `  — only ${w.visible_percent}% visible, something is in front of it`
      : '')
    + (unmeasured.length ? `  — ${listed} NOT measured here` : '')
}

export function assertOnTop(win) {
  // Disabled first: a disabled window is usually fully visible, so the
  // occlusion check would pass it through and every click after that would
  // be swallowed in silence. Cost of missing this, measured: half an hour
  // of blaming the coordinates, the timing, the model and the device, on a
  // window that was never going to accept input from anything.
  if (win.enabled === false) {
    throw new LocateError(
      `"${win.title}" is not accepting input — a dialog somewhere is modal `
      + 'over it, so every click is discarded, a physical mouse\'s included. '
      + 'It looks completely normal in a screenshot, which is why this is '
      + 'worth saying out loud. Deal with that dialog first (it may be '
      + 'behind another window, or off-screen).')
  }
  const visible = win.visible_fraction
  if (typeof visible !== 'number' || visible >= MIN_VISIBLE_FRACTION) return
  const pct = Math.round(visible * 100)
  throw new LocateError(
    `"${win.title}" is ${pct}% visible — something is in front of it, so a `
    + 'screenshot of that area would show the other window and any answer '
    + 'about it would be about the wrong application. Bring it to the front '
    + 'first: click its taskbar button, or click a part of it that is still '
    + 'showing, then try again.')
}

/** One-line human summary, which is all the agent gets back. */
export function describeResult(result, verb = 'clicked') {
  const [x, y] = result.screen
  const conf = result.answer?.target?.confidence
  const bits = [
    `${verb} (${x}, ${y}) in ${result.source}`,
    result.answer?.target?.label ? `— ${result.answer.target.label}` : '',
    conf !== undefined ? `(confidence ${conf})` : '',
    `[scale ${result.fit.x.scale.toFixed(3)}/${result.fit.y.scale.toFixed(3)},`,
    `${result.timings.totalMs}ms]`,
  ]
  const note = notPressedNote(result)
  if (note) bits.push(`— ${note}`)
  return bits.filter(Boolean).join(' ')
}
