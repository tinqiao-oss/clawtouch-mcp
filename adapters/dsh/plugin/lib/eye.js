/**
 * The "eye": hand one calibrated screenshot to a model the person configured
 * in dsh, and bring back its text answer.
 *
 * Since 0.2.0 the plugin ships no model and no key. The eye is a dsh model
 * route — `vision.provider` + `vision.model` when set, otherwise the model the
 * current conversation is using — reached through dsh's own services:
 * `ctx.attachments.saveImage()` stores the capture (dsh model calls take
 * images only as attachment references) and `ctx.llm.prepareCall()` /
 * `stream()` make the call, so credentials, gateways and provider quirks stay
 * in the person's dsh configuration.
 *
 * Everything here fails CLOSED, and before anything is pressed where that is
 * possible: `prepare()` runs before the capture and before any title-bar click
 * that brings a window forward, so a configuration that cannot work refuses
 * with no input sent at all. Three ways an answer can look fine and still be
 * wrong are refused explicitly (dsh does not refuse them for us):
 *
 *   - a route that cannot see images: dsh replaces the image with a text
 *     placeholder and dispatches anyway, and a model asked for coordinates
 *     will happily invent some;
 *   - a stream that ended in an error, a cancellation, or a truncation after
 *     printing something JSON-shaped;
 *   - a stream with no terminal chunk at all, which the assembler would
 *     otherwise report as an ordinary stop.
 */
// No host import here: this module is unit-tested without dsh installed. The
// host's BlockAssembler arrives with the services (see index.js).

export class EyeError extends Error {}

// After prepare() the capture may already have been preceded by a title-bar
// click that brought the window forward, so failures from here on say what
// is true — the target was not clicked — rather than "nothing was pressed".
const NOT_CLICKED = 'the target was not clicked'

/** Config keys of the 0.1.x direct-HTTP eye. No longer read — and refused when
 *  present, so an old profile cannot silently switch to a different model. */
export const LEGACY_VISION_KEYS = Object.freeze(['endpoint', 'apiKey', 'extraBody'])

const DEFAULT_TIMEOUT_MS = 60_000
// A batch answer for eight targets is a few hundred tokens; this leaves room
// without letting a runaway reply bill thousands.
const MAX_OUTPUT_TOKENS = 2048

const README = 'see "Choosing the eye" in the dsh-clawtouch README'

/** Legacy keys present in a `vision` block (presence, not truthiness). */
export function legacyKeys(vision) {
  if (!vision || typeof vision !== 'object') return []
  return LEGACY_VISION_KEYS.filter((k) => Object.prototype.hasOwnProperty.call(vision, k))
}

/** The message a legacy config gets, everywhere it would matter. */
export function legacyMessage(keys) {
  return `the plugin config still sets vision.${keys.join(', vision.')}, the `
    + 'direct-API settings of dsh-clawtouch 0.1.x, which 0.2.0 no longer reads. '
    + 'The eye is now a model configured in dsh itself: remove those keys and '
    + 'either leave vision unset (the current conversation model looks) or set '
    + `vision.provider and vision.model to a dsh route that accepts images (${README}). `
    + 'Nothing was pressed.'
}

/**
 * Which route looks: the configured one, else the conversation's own.
 * Read once per tool call and carried through it — `current()` reports the
 * selection for the NEXT step, which a person may change mid-run.
 */
export function resolveRoute(vision, exec) {
  const provider = vision?.provider
  const model = vision?.model
  if (provider && model) return { provider: String(provider), model: String(model), from: 'config' }
  if (provider || model) {
    throw new EyeError('vision.provider and vision.model go together: set both to '
      + `pick a dsh route, or neither to use the current conversation model (${README}). `
      + 'Nothing was pressed.')
  }
  const current = exec?.agent?.model?.current?.()
  if (!current?.provider || !current?.model) {
    throw new EyeError('no model to look with: vision.provider/vision.model are not '
      + 'set and this call carries no conversation model to borrow '
      + `(${README}). Nothing was pressed.`)
  }
  return { provider: String(current.provider), model: String(current.model), from: 'conversation' }
}

/** The effort to request: the configured one, else "off" when offered, else
 *  the lowest listed (efforts are ordered low → high), else none at all. */
export function chooseEffort(configured, info) {
  if (configured) return String(configured)
  const listed = (info?.reasoning?.efforts ?? [])
    .map((e) => (typeof e === 'string' ? e : e?.id))
    .filter(Boolean)
  if (!listed.length) return undefined
  return listed.includes('off') ? 'off' : listed[0]
}

export class DshEye {
  /**
   * @param {object} opts
   * @param {object} opts.vision  the plugin's `vision` config block
   * @param {(level: string, msg: string) => void} [opts.log]
   */
  constructor({ vision, log }) {
    this.vision = vision ?? {}
    this.log = log ?? (() => {})
    this.services = null
  }

  /** Called when dsh's `llm` and `attachments` services are available, with
   *  the host's `BlockAssembler` class: `{ llm, attachments, BlockAssembler }`. */
  attach(services) { this.services = services }
  detach() { this.services = null }

  /**
   * Everything that can be decided without pressing anything: config, the
   * services, the route, image input, the effort. Returns a prepared call.
   */
  async prepare(exec, signal) {
    const legacy = legacyKeys(this.vision)
    if (legacy.length) throw new EyeError(legacyMessage(legacy))
    const services = this.services
    if (!services?.llm || !services?.attachments || !services?.BlockAssembler) {
      throw new EyeError('this dsh profile has no model service (llm) or no '
        + 'attachment service mounted, so there is no way to show a screenshot '
        + 'to a model. Use a profile built on dsh-base. Nothing was pressed.')
    }
    const route = resolveRoute(this.vision, exec)
    let info
    let call
    try {
      call = await services.llm.prepareCall(
        { provider: route.provider, model: route.model, maxTokens: MAX_OUTPUT_TOKENS, temperature: 0 },
        signal,
        (controls, model) => {
          info = model
          const effort = chooseEffort(this.vision.effort, model)
          return effort === undefined ? controls : { ...controls, reasoningEffort: effort }
        })
    } catch (err) {
      throw new EyeError(`could not prepare the ${route.provider}/${route.model} `
        + `eye: ${err?.message ?? err}. Nothing was pressed.`)
    }
    const modalities = info?.inputModalities
    if (!Array.isArray(modalities) || !modalities.includes('image')) {
      const said = Array.isArray(modalities)
        ? `declares input [${modalities.join(', ')}]`
        : 'does not declare whether it accepts images'
      throw new EyeError(`the eye ${route.provider}/${route.model} ${said}. `
        + 'Shown a screenshot, dsh would replace it with a text placeholder and '
        + 'the model could still answer with made-up coordinates, so this is '
        + 'refused. Pick a model that sees images (vision.provider + '
        + 'vision.model), or declare `input: [text, image]` for this one in '
        + `dsh's model config if it does (${README}). Nothing was pressed.`)
    }
    return { route, call, effort: call.config.reasoningEffort }
  }

  /**
   * Show one image and an instruction to the prepared route; return its text.
   * Throws EyeError on anything but a clean, complete answer.
   */
  async ask(prepared, { imageBase64, mimeType, system, instruction, signal }) {
    const { route, call } = prepared
    const where = `${route.provider}/${route.model}`
    // One reference for the whole call: a plugin reload can detach the
    // services between awaits, and a half-swapped pair must not be used.
    const services = this.services
    if (!services?.attachments || !services?.BlockAssembler) {
      throw new EyeError(`dsh's model services went away during the look; ${NOT_CLICKED}`)
    }
    let ref
    try {
      ref = await services.attachments.saveImage({
        data: new Uint8Array(Buffer.from(imageBase64, 'base64')),
        mediaType: mimeType || 'image/jpeg',
        name: 'clawtouch-capture',
      })
    } catch (err) {
      throw new EyeError(`dsh could not store the screenshot for the ${where} eye: `
        + `${err?.message ?? err}; ${NOT_CLICKED}`)
    }
    const timeoutMs = Number(this.vision.timeoutMs) > 0 ? Number(this.vision.timeoutMs) : DEFAULT_TIMEOUT_MS
    const timeout = AbortSignal.timeout(timeoutMs)
    const combined = signal ? AbortSignal.any([signal, timeout]) : timeout
    const assembler = new services.BlockAssembler()
    let sawFinish = false
    try {
      for await (const chunk of call.stream({
        ...call.config,
        system,
        messages: [{
          role: 'user',
          content: [{ type: 'image', attachment: ref }, { type: 'text', text: instruction }],
        }],
        signal: combined,
      })) {
        assembler.push(chunk)
        if (chunk?.type === 'finish') sawFinish = true
      }
    } catch (err) {
      if (signal?.aborted) throw new EyeError(`the ${where} eye was cancelled; ${NOT_CLICKED}`)
      if (timeout.aborted) throw new EyeError(`the ${where} eye did not answer within ${timeoutMs} ms; ${NOT_CLICKED}`)
      throw new EyeError(`the ${where} eye failed: ${err?.message ?? err}; ${NOT_CLICKED}`)
    }
    if (signal?.aborted) throw new EyeError(`the ${where} eye was cancelled; ${NOT_CLICKED}`)
    if (timeout.aborted) throw new EyeError(`the ${where} eye did not answer within ${timeoutMs} ms; ${NOT_CLICKED}`)
    if (!sawFinish) {
      throw new EyeError(`the ${where} eye's reply ended without a final status, so `
        + 'whatever it said is not used; ' + NOT_CLICKED)
    }
    const finish = assembler.finish
    if (assembler.usage) {
      this.log('info', `eye ${where} usage: ${JSON.stringify(assembler.usage)}`)
    }
    if (finish?.kind !== 'stop') {
      throw new EyeError(finishMessage(where, finish))
    }
    const text = assembler.blocks()
      .filter((b) => b?.type === 'text')
      .map((b) => b.text)
      .join('')
    if (!text.trim()) throw new EyeError(`the ${where} eye answered with no text; ${NOT_CLICKED}`)
    return { text, route, effort: prepared.effort }
  }
}

function finishMessage(where, finish) {
  switch (finish?.kind) {
    case 'max-tokens':
      return `the ${where} eye's reply was cut off at the output limit, so it is `
        + 'not used; ' + NOT_CLICKED
    case 'aborted':
      return `the ${where} eye was cancelled; ${NOT_CLICKED}`
    case 'error': {
      const code = finish.failure?.code
      const msg = finish.failure?.message ?? 'unknown error'
      if (code === 'IMAGE_OFFLOAD_REQUIRED') {
        return `the ${where} route refused the screenshot as over its image budget `
          + '(IMAGE_OFFLOAD_REQUIRED); lower the plugin\'s maxWidth or raise the '
          + 'route\'s image limits in dsh; ' + NOT_CLICKED
      }
      return `the ${where} eye failed: ${msg}${code ? ` (${code})` : ''}; ${NOT_CLICKED}`
    }
    default:
      return `the ${where} eye stopped with "${finish?.kind ?? 'nothing'}" instead `
        + 'of an answer; ' + NOT_CLICKED
  }
}
