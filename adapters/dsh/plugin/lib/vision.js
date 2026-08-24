/**
 * The "eye": one OpenAI-compatible multimodal call that answers with
 * coordinates, and nothing else.
 *
 * Why a second model rather than the agent's own. The models that drive a
 * tool loop well and the models that point at pixels accurately are, as of
 * this writing, not the same models. A capable text model asked to click
 * from a screenshot misses by tens to hundreds of pixels; a capable vision
 * model asked to run an agent loop emits tool calls as prose. Splitting
 * the roles — the host's model decides *what* to do, this one answers
 * *where* — lets each do the thing it is good at, and keeps screenshots
 * out of the agent's context entirely.
 *
 * Why a direct HTTP call rather than the host's LLM service. The host can
 * route this (see README: `useHostLlm`), but that path needs a provider
 * configured with the image input modality declared by hand — a
 * silent-failure footgun — and it makes the vision step untestable
 * outside a running host. A plain endpoint + key works the same on every
 * OpenAI-compatible provider and can be exercised by a unit test.
 *
 * Zero dependencies: global `fetch`, available since Node 18.
 */

export class VisionError extends Error {}

const DEFAULT_ENDPOINT =
  'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
const DEFAULT_MODEL = 'qwen-vl-max'
const DEFAULT_TIMEOUT_MS = 60_000

const SYSTEM_PROMPT = [
  'You are a precise UI locator. You receive one screenshot and are asked',
  'for pixel coordinates inside it.',
  '',
  'Rules:',
  '- Coordinates are in the pixel space of the image you were given,',
  '  origin (0,0) at the top-left, x to the right, y down.',
  '- Point at the CENTRE of what you are asked for, not its edge or label.',
  '- If the requested element is not visible, say so with "found": false',
  '  instead of guessing a location. A wrong coordinate causes a click in',
  '  the wrong place; an honest "not found" costs one retry.',
  '- Reply with JSON only. No prose, no markdown fences.',
].join('\n')

/**
 * Ask the vision model for the two calibration markers and one or more
 * targets, in a single call.
 *
 * Calibration travels with the targets on purpose: it must describe the
 * *same* rendering of the image, and a separate calibration request would
 * double the latency and cost of every click while being no more accurate.
 *
 * Several targets travel together for the same reason plus one more.
 * Measured on a calculator: four targets in one call took 3.5s and landed
 * 2.3-5.6px from truth; the same four asked separately took 6.4s and
 * landed 3.0-5.3px — the same accuracy for half the time. The larger
 * saving is upstream: four separate clicks are also four agent round
 * trips, and those dominate a multi-step task.
 *
 * @param {object} opts
 * @param {string} opts.imageBase64
 * @param {string} opts.mimeType
 * @param {string} [opts.target]      one natural-language description
 * @param {string[]} [opts.targets]   or several, answered in order
 * @param {string} opts.markerHint    text from the screenshot metadata
 * @param {object} opts.config        { endpoint, model, apiKey, timeoutMs }
 * @param {AbortSignal} [opts.signal]
 * @returns {Promise<{markers: Record<string, [number, number]>,
 *                    target: object, targets: object[], raw: string}>}
 */
export async function locate(opts) {
  const { imageBase64, mimeType, markerHint, config } = opts
  const wanted = (opts.targets ?? [opts.target])
    .map((t) => String(t ?? '').trim())
    .filter(Boolean)
  if (!wanted.length) throw new VisionError('no target was described')
  const endpoint = config.endpoint || DEFAULT_ENDPOINT
  const model = config.model || DEFAULT_MODEL
  if (!config.apiKey) {
    throw new VisionError(
      'no vision API key configured — set the plugin\'s `vision.apiKey` '
      + 'or the DASHSCOPE_API_KEY environment variable')
  }

  const instruction = wanted.length === 1
    ? [
      markerHint,
      '',
      `Find: ${wanted[0]}`,
      '',
      'Reply with exactly this JSON shape:',
      '{"markers":{"tl":[x,y],"br":[x,y]},'
      + '"target":{"found":true,"point":[x,y],"label":"what you clicked on",'
      + '"confidence":0.0}}',
      'Both marker centres are required even when the target is not found —',
      'they are what makes your coordinates usable.',
    ].join('\n')
    : [
      markerHint,
      '',
      'Find ALL of these, independently:',
      ...wanted.map((t, i) => `  ${i + 1}. ${t}`),
      '',
      'Reply with exactly this JSON shape:',
      '{"markers":{"tl":[x,y],"br":[x,y]},"targets":['
      + '{"n":1,"found":true,"point":[x,y],"label":"what this is"}]}',
      `Include one entry for every number from 1 to ${wanted.length}, in`,
      'order, even for ones you cannot find (those get "found": false).',
      'Both marker centres are required regardless — they are what makes',
      'your coordinates usable.',
    ].join('\n')

  const body = {
    model,
    messages: [
      { role: 'system', content: SYSTEM_PROMPT },
      {
        role: 'user',
        content: [
          {
            type: 'image_url',
            image_url: { url: `data:${mimeType};base64,${imageBase64}` },
          },
          { type: 'text', text: instruction },
        ],
      },
    ],
    temperature: 0,
  }

  const controller = new AbortController()
  const timeoutMs = config.timeoutMs || DEFAULT_TIMEOUT_MS
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  if (typeof timer.unref === 'function') timer.unref()
  const onOuterAbort = () => controller.abort()
  opts.signal?.addEventListener('abort', onOuterAbort, { once: true })

  let response
  try {
    response = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${config.apiKey}`,
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    })
  } catch (err) {
    if (controller.signal.aborted) {
      throw new VisionError(`vision call aborted after ${timeoutMs}ms`)
    }
    throw new VisionError(`vision call failed: ${err.message}`)
  } finally {
    clearTimeout(timer)
    opts.signal?.removeEventListener('abort', onOuterAbort)
  }

  if (!response.ok) {
    const detail = await safeText(response)
    throw new VisionError(
      `vision endpoint returned ${response.status}: ${detail.slice(0, 400)}`)
  }
  const payload = await response.json()
  const text = payload?.choices?.[0]?.message?.content
  const content = typeof text === 'string' ? text : collectText(text)
  if (!content) {
    throw new VisionError(
      `vision endpoint returned no text: ${JSON.stringify(payload).slice(0, 300)}`)
  }
  return { ...parseAnswer(content, wanted.length), raw: content }
}

async function safeText(response) {
  try { return await response.text() } catch { return '<no body>' }
}

/** Some providers return content as an array of parts even for text. */
function collectText(content) {
  if (!Array.isArray(content)) return ''
  return content
    .map((part) => (typeof part === 'string' ? part : part?.text ?? ''))
    .join('')
}

/**
 * Pull the JSON object out of a model reply.
 *
 * Told "JSON only", models still wrap it in ```json fences or add a
 * sentence of commentary often enough that failing the whole click over
 * it would be the single largest source of flakiness. Fence-strip first,
 * then fall back to the outermost braces.
 */
export function parseAnswer(text, expected = 1) {
  const cleaned = String(text)
    .replace(/^\s*```(?:json)?/i, '')
    .replace(/```\s*$/, '')
    .trim()
  let obj
  try {
    obj = JSON.parse(cleaned)
  } catch {
    const start = cleaned.indexOf('{')
    const end = cleaned.lastIndexOf('}')
    if (start < 0 || end <= start) {
      throw new VisionError(
        `vision model did not return JSON: ${cleaned.slice(0, 300)}`)
    }
    try {
      obj = JSON.parse(cleaned.slice(start, end + 1))
    } catch (err) {
      throw new VisionError(
        `vision model returned malformed JSON (${err.message}): `
        + cleaned.slice(0, 300))
    }
  }

  const markers = {}
  for (const [id, value] of Object.entries(obj?.markers ?? {})) {
    const pt = normalizePoint(value)
    if (pt) markers[id] = pt
  }
  // The multi-target shape when the model used it, otherwise the single
  // one lifted into a one-element list so every caller reads the same
  // thing. `targets` is always present and always in the asked order.
  const byIndex = new Map()
  if (Array.isArray(obj?.targets)) {
    for (const entry of obj.targets) {
      // An index outside what was asked for is dropped rather than
      // fataled: models do emit a spurious extra entry, and losing a
      // whole batch of good answers to one stray item would be silly.
      const n = Number(entry?.n ?? entry?.index)
      if (!Number.isInteger(n) || n < 1 || n > expected) continue
      if (!byIndex.has(n)) byIndex.set(n, entry)
    }
  }
  if (!byIndex.size) byIndex.set(1, obj?.target ?? {})

  const targets = []
  for (let n = 1; n <= expected; n += 1) {
    targets.push(readTarget(byIndex.get(n) ?? {}))
  }
  return { markers, target: targets[0], targets }
}

/** One target entry, with the leniency that is safe and none that isn't. */
function readTarget(raw) {
  const point = normalizePoint(raw.point ?? raw.center)
  // A target counts as found only when the model did not say otherwise
  // AND gave a usable point. `found` is checked through `isDenial` because
  // models emit the string "false" and the word "no" as often as the
  // boolean, and `"false" !== false` — which quietly turned an explicit
  // "it is not on screen" into a click.
  return {
    found: !isDenial(raw.found) && Boolean(point),
    point: point ?? undefined,
    label: typeof raw.label === 'string' ? raw.label : undefined,
    confidence: typeof raw.confidence === 'number'
      && Number.isFinite(raw.confidence) ? raw.confidence : undefined,
  }
}

/** True when the model said the target is NOT there, in any of the forms
 *  it says it in. Anything else (including a missing field) is not a
 *  denial — the point itself then decides. */
function isDenial(value) {
  if (value === false) return true
  if (typeof value === 'string') {
    return ['false', 'no', 'none', 'null', '0'].includes(value.trim().toLowerCase())
  }
  if (value === 0 || value === null) return true
  return false
}

/**
 * Accept `[x,y]`, `{x,y}`, and `{"0":x,"1":y}` — all three occur.
 *
 * Coercion is deliberately narrow. `Number(null)` is 0 and `Number('')`
 * is 0, so a lenient parser turns a model that answered `[null, null]`
 * into a confident click on the image's top-left corner. Only real
 * numbers, and numeric strings that actually contain a number, count.
 */
function normalizePoint(value) {
  if (Array.isArray(value) && value.length >= 2) {
    const x = coordinate(value[0])
    const y = coordinate(value[1])
    return x === null || y === null ? null : [x, y]
  }
  if (value && typeof value === 'object') {
    const x = coordinate(value.x !== undefined ? value.x : value['0'])
    const y = coordinate(value.y !== undefined ? value.y : value['1'])
    return x === null || y === null ? null : [x, y]
  }
  return null
}

function coordinate(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    const n = Number(value)
    return Number.isFinite(n) ? n : null
  }
  return null
}
