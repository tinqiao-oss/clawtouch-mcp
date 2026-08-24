/**
 * A minimal MCP stdio client: enough to own one `clawtouch-mcp` process.
 *
 * Why not reuse the host's MCP client. The plugin needs the *screenshot
 * bytes in-process* — it sends them to a vision model itself and never
 * puts an image into the agent's context. A host-managed MCP connection
 * hands tool results to the model, not to us. It also cannot be shared:
 * the serial port behind clawtouch-mcp is exclusive, so a second
 * connection to the same device is not a second client, it is a conflict.
 * Owning the process is what makes "one tool call in, one text answer
 * out" possible.
 *
 * Framing is newline-delimited JSON — clawtouch-mcp's stdio loop reads a
 * line at a time and only switches to Content-Length framing if the first
 * message asks for it.
 *
 * The child is kept UNREFERENCED while idle. A piped child process and its
 * three stdio streams each hold a handle that keeps Node's event loop
 * alive, so a host that finished its work would sit there, done, unable to
 * exit, for as long as the device stayed connected. Measured before the
 * fix: a one-shot `dsh` run answered in 6 seconds and then hung until it
 * was killed two minutes later — which reads as "this tool is extremely
 * slow" and is nothing of the kind. Handles are re-referenced while a
 * request is in flight, so the loop can never exit mid-call.
 *
 * Zero dependencies: node: builtins only.
 */
import { spawn } from 'node:child_process'

const PROTOCOL_VERSION = '2024-11-05'

/** How long any single tool call may take before we give up on it. */
const DEFAULT_CALL_TIMEOUT_MS = 30_000

export class McpProcessError extends Error {}

export class McpStdioClient {
  /**
   * @param {object} opts
   * @param {string} opts.command  executable (e.g. `clawtouch-mcp` or `python`)
   * @param {string[]} [opts.args]
   * @param {Record<string,string>} [opts.env] merged over process.env
   * @param {string} [opts.cwd]
   * @param {(level: string, msg: string) => void} [opts.log]
   * @param {number} [opts.callTimeoutMs]
   */
  constructor(opts) {
    this.command = opts.command
    this.args = opts.args ?? []
    this.env = opts.env
    this.cwd = opts.cwd
    this.log = opts.log ?? (() => {})
    this.callTimeoutMs = opts.callTimeoutMs ?? DEFAULT_CALL_TIMEOUT_MS
    this.child = null
    this.tools = []
    this._nextId = 1
    this._pending = new Map()
    this._buffer = ''
    this._starting = null
    /** Last few stderr lines — the only useful thing to show when the
     *  process dies during startup (missing device, bad flag, no mss). */
    this._stderrTail = []
  }

  get running() {
    return this.child !== null && this.child.exitCode === null
  }

  /** Idempotent, and safe to call concurrently: callers share one start. */
  async start() {
    if (this.running) return
    if (this._starting) return this._starting
    this._starting = this._start().finally(() => { this._starting = null })
    return this._starting
  }

  async _start() {
    const child = spawn(this.command, this.args, {
      cwd: this.cwd,
      env: this.env ? { ...process.env, ...this.env } : process.env,
      stdio: ['pipe', 'pipe', 'pipe'],
      // Never a shell: the command and args come from user config, and a
      // shell would make a path with a space (very common on Windows) into
      // an argument-splitting bug at best.
      shell: false,
    })
    this.child = child
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => this._onStdout(chunk))
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => {
      for (const line of String(chunk).split(/\r?\n/)) {
        if (!line.trim()) continue
        this._stderrTail.push(line)
        if (this._stderrTail.length > 20) this._stderrTail.shift()
        this.log('debug', `clawtouch-mcp: ${line}`)
      }
    })
    child.on('exit', (code, signal) => {
      const why = signal ? `signal ${signal}` : `code ${code}`
      this._failAllPending(new McpProcessError(
        `clawtouch-mcp exited (${why})${this._stderrHint()}`))
      this.child = null
    })
    child.on('error', (err) => {
      this._failAllPending(new McpProcessError(
        `could not run "${this.command}": ${err.message}`))
      this.child = null
    })
    this._updateHandleRefs()

    const init = await this._request('initialize', {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: 'dsh-clawtouch', version: '0.0.0' },
    })
    this._notify('notifications/initialized', {})
    const listed = await this._request('tools/list', {})
    this.tools = listed?.tools ?? []
    this.log('info', `clawtouch-mcp ready: ${this.tools.length} tools `
      + `(${init?.serverInfo?.name ?? 'unknown'} `
      + `${init?.serverInfo?.version ?? '?'})`)
  }

  hasTool(name) {
    return this.tools.some((t) => t.name === name)
  }

  /**
   * Call one MCP tool. Returns `{ text, json, images, isError }`:
   * clawtouch-mcp answers with a text part carrying JSON metadata and,
   * for screenshots, an image part. Splitting them here keeps every
   * caller from re-deriving the same shape.
   */
  async callTool(name, args) {
    await this.start()
    const result = await this._request('tools/call', {
      name, arguments: args ?? {},
    })
    const content = result?.content ?? []
    const texts = []
    const images = []
    for (const part of content) {
      if (part?.type === 'text') texts.push(part.text)
      else if (part?.type === 'image') {
        images.push({ data: part.data, mimeType: part.mimeType })
      }
    }
    const text = texts.join('\n')
    let json
    try { json = JSON.parse(text) } catch { json = undefined }
    return { text, json, images, isError: Boolean(result?.isError) }
  }

  async stop() {
    const child = this.child
    if (!child) return
    this.child = null
    this._failAllPending(new McpProcessError('client stopped'))
    try { child.stdin.end() } catch { /* already closed */ }
    // Give it a moment to exit on EOF (it releases the serial port on the
    // way out); only then insist.
    await new Promise((resolve) => {
      const timer = setTimeout(() => { try { child.kill() } catch {} ; resolve() }, 2000)
      child.once('exit', () => { clearTimeout(timer); resolve() })
    })
  }

  // ── internals ──

  _stderrHint() {
    if (!this._stderrTail.length) return ''
    return `\nlast output:\n  ${this._stderrTail.slice(-5).join('\n  ')}`
  }

  _onStdout(chunk) {
    this._buffer += chunk
    let idx
    while ((idx = this._buffer.indexOf('\n')) >= 0) {
      const line = this._buffer.slice(0, idx).trim()
      this._buffer = this._buffer.slice(idx + 1)
      if (!line) continue
      let msg
      try {
        msg = JSON.parse(line)
      } catch {
        // A non-JSON line on stdout is a server-side bug (something
        // printed past the protocol). Log it rather than killing the
        // session over it.
        this.log('warn', `clawtouch-mcp: non-JSON stdout: ${line.slice(0, 200)}`)
        continue
      }
      const entry = msg.id != null ? this._pending.get(msg.id) : undefined
      if (!entry) continue      // notification, or a response we no longer await
      this._pending.delete(msg.id)
      this._updateHandleRefs()
      clearTimeout(entry.timer)
      if (msg.error) {
        entry.reject(new McpProcessError(
          `${msg.error.message ?? 'MCP error'} (code ${msg.error.code})`))
      } else {
        entry.resolve(msg.result)
      }
    }
  }

  _write(obj) {
    if (!this.child) throw new McpProcessError('clawtouch-mcp is not running')
    this.child.stdin.write(`${JSON.stringify(obj)}\n`)
  }

  _notify(method, params) {
    this._write({ jsonrpc: '2.0', method, params })
  }

  _request(method, params) {
    const id = this._nextId++
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pending.delete(id)
        this._updateHandleRefs()
        reject(new McpProcessError(
          `${method} timed out after ${this.callTimeoutMs}ms`))
      }, this.callTimeoutMs)
      // `unref` so a pending call can never hold the host process open.
      if (typeof timer.unref === 'function') timer.unref()
      this._pending.set(id, { resolve, reject, timer })
      this._updateHandleRefs()
      try {
        this._write({ jsonrpc: '2.0', id, method, params })
      } catch (err) {
        this._pending.delete(id)
        clearTimeout(timer)
        this._updateHandleRefs()
        reject(err)
      }
    })
  }

  _failAllPending(err) {
    for (const [, entry] of this._pending) {
      clearTimeout(entry.timer)
      entry.reject(err)
    }
    this._pending.clear()
    this._updateHandleRefs()
  }

  /**
   * Hold the event loop open only while we are actually waiting on the
   * child. Idle, every handle is released so the host can exit the moment
   * its own work is done; busy, they are held so a reply can never be lost
   * to an exit mid-call.
   */
  _updateHandleRefs() {
    const child = this.child
    if (!child) return
    const busy = this._pending.size > 0
    for (const stream of [child.stdout, child.stderr, child.stdin]) {
      if (!stream) continue
      const fn = busy ? stream.ref : stream.unref
      if (typeof fn === 'function') fn.call(stream)
    }
    const own = busy ? child.ref : child.unref
    if (typeof own === 'function') own.call(child)
  }
}

export default McpStdioClient
