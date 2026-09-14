/**
 * What `computer_type` can actually type, checked before anything is sent.
 *
 * `hid.type` hands the text to the device's firmware, which presses one key
 * per character on a US keyboard layout. A character with no key on that
 * layout — Chinese, an emoji, a curly quote, "é", a no-break space — makes
 * the firmware stop AT that character, after every character before it has
 * already been typed. The server also sends text in 32-character chunks and
 * goes on to the next chunk after a failed one. So a mixed string used to
 * arrive as fragments, with an error attached, in a field the agent then
 * reasoned about as if nothing had happened.
 *
 * Checking the whole text first turns that into a refusal with nothing
 * typed and nothing clicked. Control characters are left alone on purpose:
 * the server strips them itself (a stray newline must not submit a chat
 * draft) and reports how many characters it actually sent.
 */

/**
 * The distinct characters in `text` that a US keyboard layout cannot type,
 * in the order they first appear. Empty when the whole text is typeable.
 *
 * Iterates by code point, so an emoji is one character here, not the two
 * UTF-16 halves `text[i]` would give.
 */
export function untypableChars(text) {
  const found = new Set()
  for (const ch of String(text)) {
    // 0x00-0x7F: printable ASCII is on the layout; C0 controls and DEL are
    // stripped by the server before anything goes out.
    if (ch.codePointAt(0) > 0x7f) found.add(ch)
  }
  return [...found]
}

const SHOWN = 8

/** The refusal the agent reads — what was wrong and what it can do. */
export function untypableMessage(chars) {
  const shown = chars.slice(0, SHOWN).map((c) => JSON.stringify(c)).join(' ')
  const more = chars.length > SHOWN ? ` and ${chars.length - SHOWN} more` : ''
  return `nothing was typed or clicked: the text contains ${shown}${more}, `
    + 'which a US keyboard layout has no key for. The device types one key '
    + 'per character, so it would have stopped at the first of them with the '
    + 'text half entered. Rewrite those in plain ASCII if the text allows it '
    + '(straight quotes, "-" for a dash); Chinese and other non-ASCII text '
    + 'cannot be typed through this device.'
}
