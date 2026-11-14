/**
 * Which tool executions are under way, and whether new ones may start.
 *
 * A tool execution is the whole `computer_*` call: the capture, the look by
 * the eye (seconds, with nothing in flight to clawtouch-mcp), and the input.
 * Two things must not happen inside one:
 *
 *   - a save from the settings page. dsh reloads the plugin with the new
 *     config, and an execution of the old instance that was waiting on the
 *     eye would carry on afterwards with the OLD settings — including
 *     pressing after "look, don't press" was just turned on. So a save is
 *     refused while one runs, and closes admission before it awaits
 *     anything.
 *   - the page's device check slipping between a capture and the click
 *     that capture was for, making the click land on an older picture of
 *     the screen. So the check is skipped while one runs.
 *
 * Disposal closes admission for good: whatever is left of an old instance
 * must not press anything after the plugin has been replaced.
 */
export class ActivityGate {
  constructor() {
    this.active = 0
    /** Why new executions are refused, or null. */
    this.closedFor = null
    this._final = false
  }

  /** Run one tool execution; refuse it outright while admission is closed. */
  async run(fn) {
    if (this.closedFor !== null) {
      throw new Error(`dsh-clawtouch is ${this.closedFor}, so this call was refused `
        + 'before anything was pressed. Try it again in a moment.')
    }
    this.active += 1
    try {
      return await fn()
    } finally {
      this.active -= 1
    }
  }

  /**
   * Close admission if nothing runs, synchronously — the caller must do this
   * before its first await. Returns a function that reopens it (a no-op once
   * the gate is closed for good), or null when an execution is under way or
   * admission is already closed.
   */
  closeIfIdle(reason) {
    if (this.active > 0 || this.closedFor !== null) return null
    this.closedFor = reason
    return () => {
      if (!this._final && this.closedFor === reason) this.closedFor = null
    }
  }

  /** Close admission for good: the plugin instance is going away. */
  closeForGood(reason) {
    this._final = true
    this.closedFor = reason
  }

  get idle() {
    return this.active === 0 && this.closedFor === null
  }
}
