/**
 * Which key combinations the plugin refuses to press, and why.
 *
 * A USB HID keystroke has no application-level address: it lands on
 * whatever window has focus. On the machine the agent itself runs on, two
 * kinds of combination can end the task, so both are refused by default:
 *
 *   quit  — Cmd+Q, Alt+F4, Cmd+W close whatever is in front, which may be
 *           the agent's own window.
 *   focus — Alt+Tab, the Windows key, Cmd+Tab and friends move focus to
 *           another window. Every later keystroke then goes there, and the
 *           agent has no reliable way back. Seen 2026-09-13: an agent
 *           pressed Alt+Tab mid-task, focus went to the editor behind the
 *           task window, and the rest of the run never got it back.
 *
 * clawtouch-mcp accepts shorthand in `key` — "alt+f4" means F4 held with
 * Alt (server.py `_split_key_shorthand`). A combination is therefore
 * normalised the way the server will parse it before it is matched;
 * matching the raw `key` alone let "alt+f4" walk straight past the check.
 *
 * Pure: no host, no device. `platform` decides only what the GUI key means
 * (the Windows key, where every combination is a shell shortcut, versus
 * Command on macOS, where most are ordinary application shortcuts).
 */

// The head tokens server.py `_split_key_shorthand` treats as modifiers.
const SHORTHAND_MODIFIERS = new Set(['ctrl', 'shift', 'alt', 'gui', 'win', 'cmd'])

// Names folded together here. The server only accepts some of them; folding
// the rest too costs nothing, and a guard should not depend on which
// spellings happen to be valid today.
const MODIFIER_ALIAS = {
  control: 'ctrl',
  win: 'gui', cmd: 'gui', command: 'gui', meta: 'gui', super: 'gui', windows: 'gui',
  option: 'alt', opt: 'alt',
}
const KEY_ALIAS = {
  escape: 'esc', return: 'enter', del: 'delete',
  backtick: 'grave', '`': 'grave', tilde: 'grave', '~': 'grave',
}

// Python's str.strip() and JavaScript's trim() disagree on what whitespace
// is: Python also strips U+001C-U+001F and U+0085, JavaScript also strips
// U+FEFF. The server strips key and modifier names with Python, so
// "tab" held with Alt is pressed as Alt+Tab — while trim() left the
// guard looking at a key it did not know (found by the GPT review of this
// change, 2026-09-14). Strip the union: the guard then sees at least what
// the server sees, and a spelling the server would reject is merely refused.
const EDGE_SPACE = /^[\s\x1c-\x1f\x85]+|[\s\x1c-\x1f\x85]+$/g
const strip = (s) => s.replace(EDGE_SPACE, '')

const foldModifier = (name) => {
  const n = strip(name).toLowerCase()
  return MODIFIER_ALIAS[n] ?? n
}

/**
 * Normalise a `computer_key` call into the combination the server will send:
 * the same key, and the same Ctrl/Alt/GUI modifiers. (The server also adds
 * Shift for shifted glyphs such as "Q" or "plus"; no rule here reads Shift,
 * so that difference cannot change a verdict.)
 * `key` and `modifiers` are taken as the plugin forwards them: a missing key
 * is rejected by the server, any other key value is stringified by it
 * (`str(kw["key"])` — a numeric 1 held with Win is Win+1), and a
 * non-array `modifiers` is sent as none at all (index.js computer_key).
 * @returns {{ key: string, mods: Set<string> } | null} null when there is
 *   no key (nothing is pressed).
 */
export function normalizeCombo(key, modifiers) {
  if (key === undefined || key === null) return null
  const mods = new Set()
  for (const m of Array.isArray(modifiers) ? modifiers : []) {
    // a non-string item makes the server raise before pressing anything
    if (typeof m === 'string') mods.add(foldModifier(m))
  }
  let k = String(key)
  if (k.length > 1 && k.includes('+')) {
    const parts = k.split('+')
    const head = parts.slice(0, -1)
    const tail = parts[parts.length - 1]
    // Looser than the server's split (it does not strip the head tokens):
    // refusing a spelling the server would have rejected anyway is
    // harmless; the reverse is not.
    if (tail && head.every((p) => SHORTHAND_MODIFIERS.has(strip(p).toLowerCase()))) {
      for (const p of head) mods.add(foldModifier(p))
      k = tail
    }
  }
  if (k === ' ') return { key: 'space', mods }
  const t = strip(k).toLowerCase()
  return { key: KEY_ALIAS[t] ?? t, mods }
}

// The quit class is "close whatever is in front", spelled per platform: on
// macOS that is Cmd+Q / Cmd+W; on Windows and Linux desktops Alt+F4, plus
// Ctrl+W / Ctrl+F4, the same close-this-window/tab gesture as Cmd+W (a
// browser showing the dsh web UI closes it). A GUI combination is never a
// quit combo off macOS — Win+Q is the shell's search — so it falls to the
// focus class instead, with a message that names what is really pressed.
function quitCombo(k, mods, platform) {
  const gui = mods.has('gui')
  if (platform === 'darwin') {
    if (k === 'q' && gui) return 'Cmd+Q (quit application)'
    if (k === 'w' && gui) return 'Cmd+W (close window)'
    return null
  }
  if (k === 'f4' && mods.has('alt')) return 'Alt+F4 (close window)'
  if (gui) return null
  if ((k === 'w' || k === 'f4') && mods.has('ctrl')) return `Ctrl+${k.toUpperCase()} (close window or tab)`
  return null
}

const ARROWS = new Set(['up', 'down', 'left', 'right'])
const MAC_COMMAND_SWITCHES = {
  tab: 'Cmd+Tab (switch application)',
  space: 'Cmd+Space (Spotlight)',
  grave: 'Cmd+` (cycle windows)',
  h: 'Cmd+H (hide application)',
  m: 'Cmd+M (minimise window)',
}

function focusCombo(k, mods, platform) {
  const gui = mods.has('gui')
  const alt = mods.has('alt')
  const ctrl = mods.has('ctrl')
  if (platform === 'darwin') {
    // Most Cmd combinations are the focused application's own shortcuts
    // (copy, paste, save), so only the ones that leave the app are refused.
    if (gui && alt && k === 'esc') return 'Cmd+Option+Esc (force quit dialog)'
    if (gui && MAC_COMMAND_SWITCHES[k]) return MAC_COMMAND_SWITCHES[k]
    if (ctrl && ARROWS.has(k)) return `Ctrl+${k} (Mission Control / switch Space)`
    if (ctrl && (k === 'f2' || k === 'f3')) return `Ctrl+${k.toUpperCase()} (moves keyboard focus to the ${k === 'f2' ? 'menu bar' : 'Dock'})`
    return null
  }
  // Windows and Linux desktops.
  if (k === 'tab' && alt) return 'Alt+Tab (switch window)'
  if (k === 'esc' && alt) return 'Alt+Esc (cycle windows)'
  if (k === 'esc' && ctrl) return 'Ctrl+Esc / Ctrl+Shift+Esc (Start menu / Task Manager)'
  if (k === 'delete' && ctrl && alt) return 'Ctrl+Alt+Del (security screen)'
  // The shell owns every GUI-key combination: Start, task view, show
  // desktop, run, lock, snap, switch to the n-th taskbar app.
  if (gui) {
    const name = platform === 'linux' ? 'Super' : 'Win'
    return `${name}+${k.toUpperCase()} (a ${name}-key shortcut, handled by the desktop shell)`
  }
  return null
}

/**
 * @param {unknown} key       `computer_key`'s `key` argument, as sent.
 * @param {unknown} modifiers `computer_key`'s `modifiers` argument, as sent.
 * @param {{platform?: string, quit?: boolean, focus?: boolean}} [opts]
 *   `quit` / `focus` enable each class (both default on).
 * @returns {{kind: 'quit'|'focus', what: string} | null} why the combination
 *   is refused, or null to let it through.
 */
export function refusedCombo(key, modifiers, { platform = process.platform, quit = true, focus = true } = {}) {
  const combo = normalizeCombo(key, modifiers)
  if (!combo) return null
  if (quit) {
    const what = quitCombo(combo.key, combo.mods, platform)
    if (what) return { kind: 'quit', what }
  }
  if (focus) {
    const what = focusCombo(combo.key, combo.mods, platform)
    if (what) return { kind: 'focus', what }
  }
  return null
}
