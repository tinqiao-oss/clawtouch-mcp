/**
 * Turning a vision model's coordinates into a screen point.
 *
 * There are three coordinate spaces in play and only two of them are
 * knowable in advance:
 *
 *   screen  — what hid.click takes. Known.
 *   image   — the pixels we captured and encoded. Known: we chose the
 *             crop and the width.
 *   model   — whatever the model rescaled the image to internally. NOT
 *             known, NOT reported by any provider, and different across
 *             models and input sizes.
 *
 * The markers close that last gap. Two shapes are drawn at positions we
 * chose, so their `image` coordinates are exact. Asking the model where
 * those same shapes are yields their `model` coordinates. Two points per
 * axis determine the affine map between the two spaces:
 *
 *     model = scale * image + offset
 *
 * and the target comes back through its inverse. A one-point ratio
 * (`model / image`) would work only if the offset were exactly zero —
 * true for a pure resize, false the moment the model letterboxes, pads to
 * a tile size, or crops a margin, all of which are common.
 *
 * Everything here is pure arithmetic: no I/O, no model, fully testable.
 */

/** Refuse a fit whose scale is outside this range — a value this far off
 *  means a marker was misread, not that the model resized unusually. */
const MIN_SCALE = 0.05
const MAX_SCALE = 20

/**
 * How far the observed anisotropy may stray from the nearest PLAUSIBLE
 * rescale before the fit is called suspect.
 *
 * A flat "the two axes must agree" rule looked right and is wrong: some
 * vision models normalise each axis independently onto a fixed square
 * (0-1000 by 0-1000 is a common convention), so a 1600x900 image
 * legitimately comes back with x and y scales 44% apart. Rejecting that
 * would refuse to work with those models at all — a far worse failure
 * than the misread marker the check is for.
 *
 * So two hypotheses are allowed and the fit only has to match ONE:
 * isotropic (the scales agree) or square-normalised (the scales map both
 * sides of the image to the same length). A misread marker matches
 * neither.
 */
const MAX_MODEL_SKEW = 0.25

/** The marker ids this calibration is defined in terms of. Reading them
 *  BY NAME rather than taking whatever two keys the model emitted, in
 *  whatever order, is what makes a swapped pair detectable. */
const REQUIRED_IDS = ['tl', 'br']

export class CalibrationError extends Error {}

/**
 * Fit `model = scale * image + offset` on one axis from two point pairs.
 * @returns {{scale: number, offset: number}}
 */
function fitAxis(imageA, imageB, modelA, modelB, axisName) {
  const dImage = imageB - imageA
  const dModel = modelB - modelA
  if (Math.abs(dImage) < 1e-6) {
    throw new CalibrationError(
      `markers are at the same ${axisName} position; cannot calibrate`)
  }
  const scale = dModel / dImage
  // A NEGATIVE scale means the model reported the two markers the other
  // way round. Nothing downstream notices: the fit inverts cleanly, the
  // axes stay consistent with each other, and every target comes back
  // mirrored through the centre of the image — a confident click on the
  // wrong thing, which is the exact failure this design exists to
  // prevent. It is never a legitimate rescale.
  if (!Number.isFinite(scale) || scale <= 0) {
    const shown = Number.isFinite(scale) ? scale.toFixed(3) : String(scale)
    throw new CalibrationError(
      `${axisName} scale came out ${shown} — the model reported the `
      + 'calibration markers in the wrong order, so every coordinate from '
      + 'it would be mirrored')
  }
  if (scale < MIN_SCALE || scale > MAX_SCALE) {
    throw new CalibrationError(
      `implausible ${axisName} scale ${scale.toFixed(3)} — the model `
      + 'probably did not find both calibration markers')
  }
  return { scale, offset: modelA - scale * imageA }
}

/**
 * Build the model→image transform from the two markers.
 *
 * @param {Array<{id: string, center: [number, number]}>} markers
 *   as reported by `hid.screenshot` — exact image-space centres.
 * @param {Record<string, [number, number]>} reported
 *   what the vision model said, keyed by marker id.
 */
export function calibrate(markers, reported, imageSize) {
  const byId = new Map(markers.map((m) => [m.id, m]))
  const pts = REQUIRED_IDS.map((id) => {
    const marker = byId.get(id)
    if (!marker) {
      throw new CalibrationError(
        `the image has no "${id}" marker (it has `
        + `${[...byId.keys()].join(', ') || 'none'})`)
    }
    return { marker, pt: markerPoint(reported && reported[id], id) }
  })
  const [a, b] = pts

  const x = fitAxis(a.marker.center[0], b.marker.center[0],
    a.pt[0], b.pt[0], 'x')
  const y = fitAxis(a.marker.center[1], b.marker.center[1],
    a.pt[1], b.pt[1], 'y')

  const skew = plausibilitySkew(x.scale, y.scale, imageSize)
  if (skew > MAX_MODEL_SKEW) {
    throw new CalibrationError(
      `the x and y scales (${x.scale.toFixed(3)}, ${y.scale.toFixed(3)}) `
      + 'match neither a proportional resize nor a per-axis normalisation '
      + '— one marker was probably misread; refusing to click on this fit')
  }
  return { x, y, skew }
}

/** One reported marker centre, or a refusal naming what came back. */
function markerPoint(value, id) {
  if (!Array.isArray(value) || value.length < 2) {
    throw new CalibrationError(
      `marker "${id}" came back as ${JSON.stringify(value)}; both marker `
      + 'centres are required to convert any coordinate')
  }
  const [x, y] = value
  if (typeof x !== 'number' || typeof y !== 'number'
      || !Number.isFinite(x) || !Number.isFinite(y)) {
    throw new CalibrationError(
      `marker "${id}" came back as ${JSON.stringify(value)}, which is not a `
      + 'pair of numbers')
  }
  return [x, y]
}

/**
 * Distance from the nearest plausible rescale, as a fraction.
 *
 * Hypothesis A — proportional resize: the two scales are equal.
 * Hypothesis B — per-axis normalisation onto a common length: the scales
 *   map both sides of the image to the same number (`kx*W === ky*H`).
 *
 * Without `imageSize` only A can be tested, which is the older behaviour.
 */
function plausibilitySkew(kx, ky, imageSize) {
  const isotropic = Math.abs(kx - ky) / Math.max(kx, ky)
  if (!imageSize || !(imageSize.width > 0) || !(imageSize.height > 0)) {
    return isotropic
  }
  const sideX = kx * imageSize.width
  const sideY = ky * imageSize.height
  const squared = Math.abs(sideX - sideY) / Math.max(sideX, sideY)
  return Math.min(isotropic, squared)
}

/** Invert the fit: a point the model reported, back into image pixels. */
export function toImagePoint(fit, point) {
  return [
    (point[0] - fit.x.offset) / fit.x.scale,
    (point[1] - fit.y.offset) / fit.y.scale,
  ]
}

/**
 * Image pixels → the screen coordinates hid.click takes.
 *
 * `captureRect` and `imageScale` both come straight from the screenshot
 * metadata, so this stays correct across region crops, `max_width`, and
 * Retina captures without the caller knowing which of those happened.
 */
export function toScreenPoint(imagePoint, captureRect, imageScale) {
  const [left, top] = captureRect
  const [sx, sy] = imageScale
  if (!(sx > 0) || !(sy > 0)) {
    throw new CalibrationError(
      `screenshot reported a non-positive image_scale [${sx}, ${sy}]`)
  }
  return [
    Math.round(left + imagePoint[0] / sx),
    Math.round(top + imagePoint[1] / sy),
  ]
}

/**
 * Full pipeline for one reported point, with the bounds check that keeps
 * a hallucinated coordinate from becoming a click somewhere random.
 */
export function resolvePoint(fit, reportedPoint, meta) {
  const raw = toImagePoint(fit, reportedPoint)
  // The tolerance is for sub-pixel rounding at the very edge, nothing
  // more, so it is applied as a CLAMP rather than as a wider acceptance
  // window: letting a point a couple of pixels past the edge through
  // unchanged yields a screen coordinate outside the rectangle anyone
  // actually looked at.
  const margin = 2
  const maxX = meta.width - 1
  const maxY = meta.height - 1
  if (raw[0] < -margin || raw[1] < -margin
      || raw[0] > maxX + margin || raw[1] > maxY + margin) {
    throw new CalibrationError(
      `target maps to (${raw[0].toFixed(0)}, ${raw[1].toFixed(0)}) which `
      + `is outside the ${meta.width}x${meta.height} capture — the model `
      + 'reported a point it could not have seen')
  }
  const image = [
    Math.min(Math.max(raw[0], 0), maxX),
    Math.min(Math.max(raw[1], 0), maxY),
  ]
  const screen = toScreenPoint(image, meta.capture_rect, meta.image_scale)
  return { image, screen }
}
