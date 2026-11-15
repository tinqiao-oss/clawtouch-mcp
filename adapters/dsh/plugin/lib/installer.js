/**
 * Finding and installing clawtouch-mcp for the settings page.
 *
 * The plugin talks to the board through clawtouch-mcp, a Python program from
 * PyPI. A person who installs the plugin from dsh's Plugins page has usually
 * never heard of it, so the page can find one already installed, or install
 * one — but only for people who have Python 3.10+: this does not install
 * Python itself.
 *
 *   detect()   which Pythons are here, and which clawtouch-mcp each has
 *   Installer  one job at a time: a venv of the plugin's own under the dsh
 *              home, and clawtouch-mcp[screenshot,window]==MCP_VERSION in it,
 *              from PyPI and, if that fails, once more from the Tsinghua mirror
 *
 * Detection runs only when the page shows its install card (clawtouch-mcp
 * cannot run, or is too old); nothing goes online unless the person clicks
 * Install. No path or argument from the page reaches a spawn: the page picks
 * a Python by id from one detection, and the interpreter path that detection
 * probed is what runs.
 *
 * Every child is spawned without a shell, hidden (dsh desktop is a GUI
 * process; a console child would get a console window), with a deadline, and
 * killed with its whole process tree when cancelled. Python runs with
 * `-X utf8 -E` (UTF-8 output even on a Chinese Windows code page; PYTHON*
 * variables ignored) and pip with `--isolated` and an explicit index (the
 * person's pip configuration cannot send the install elsewhere or add
 * sources).
 */
import { spawn } from 'node:child_process'
import { closeSync, existsSync, lstatSync, mkdirSync, openSync, readdirSync, rmSync, statSync, unlinkSync,
  writeFileSync, writeSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, posix, resolve, win32 } from 'node:path'

export const MCP_PACKAGE = 'clawtouch-mcp'
/** The clawtouch-mcp this plugin was tested with; device.status needs 0.6.1. */
export const MCP_VERSION = '0.6.1'
/** With the extras the plugin needs: screenshots (mss, Pillow), and the
 *  window list on macOS (pyobjc Quartz; the extra is empty elsewhere). */
export const MCP_SPEC = `${MCP_PACKAGE}[screenshot,window]==${MCP_VERSION}`
export const MIN_PYTHON = [3, 10]
export const INDEXES = Object.freeze([
  ['pypi', 'https://pypi.org/simple'],
  ['tuna', 'https://pypi.tuna.tsinghua.edu.cn/simple'],
])
export const MIRROR = INDEXES[1][1]

const PY = ['-X', 'utf8', '-E']
const PROBE_MS = 8_000
const STEP_MS = 4 * 60_000
/** One source's share of an install: a PyPI that does not answer in this long gives way to the mirror. */
const PIP_TRY_MS = 150_000
const LOG_LINES = 40
const LINE_MAX = 300
const LOCK_STALE_MS = 15 * 60_000
const KILL_GRACE_MS = 10_000
const MARKER = '.dsh-clawtouch'

/** Path rules of the platform being described, not of the one running the
 *  code (the tests describe all three). */
const pathsFor = (platform) => (platform === 'win32' ? win32 : posix)

/**
 * Run under each candidate. Reports the version, whether venv (with
 * ensurepip: Debian ships it separately) is there, the clawtouch-mcp this
 * interpreter already has with its console script, and whether the
 * screenshot dependency is importable.
 */
export const PROBE = [
  'import json, os, sys, sysconfig',
  'from importlib import util',
  'out = {"version": list(sys.version_info[:3]), "executable": sys.executable,',
  '       "invenv": sys.prefix != sys.base_prefix}',
  'out["venv"] = util.find_spec("venv") is not None and util.find_spec("ensurepip") is not None',
  'out["pip"] = util.find_spec("pip") is not None',
  'def can_import(name):',
  '    try:',
  '        __import__(name)',
  '        return True',
  '    except Exception:',
  '        return False',
  'out["mss"] = can_import("mss")',
  'out["quartz"] = can_import("Quartz") if sys.platform == "darwin" else None',
  'try:',
  '    from importlib import metadata',
  '    out["mcp"] = metadata.version("clawtouch-mcp")',
  'except Exception:',
  '    out["mcp"] = None',
  'names = ["clawtouch-mcp.exe", "clawtouch-mcp"] if os.name == "nt" else ["clawtouch-mcp"]',
  'dirs = []',
  'for scheme in [None, os.name + "_user", "osx_framework_user"]:',
  '    try:',
  '        d = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")',
  '    except Exception:',
  '        continue',
  '    if d and d not in dirs:',
  '        dirs.append(d)',
  'out["scripts"] = [os.path.join(d, n) for d in dirs for n in names if os.path.isfile(os.path.join(d, n))]',
  'print(json.dumps(out))',
].join('\n')

/** dsh's home, the way dsh resolves it: DSH_HOME, else ~/.dsh. */
export function dshHome(env = process.env) {
  const value = env.DSH_HOME
  if (typeof value !== 'string' || !value.trim()) return join(homedir(), '.dsh')
  if (value === '~') return homedir()
  if (value.startsWith('~/') || value.startsWith('~\\')) return join(homedir(), value.slice(2))
  return resolve(value)
}

/** The plugin's own install: one venv under the dsh home, shared by every profile. */
export function managedPaths({ home = dshHome(), platform = process.platform } = {}) {
  const P = pathsFor(platform)
  const root = P.join(home, 'clawtouch-mcp')
  const venv = P.join(root, 'venv')
  const win = platform === 'win32'
  return {
    root,
    venv,
    python: win ? P.join(venv, 'Scripts', 'python.exe') : P.join(venv, 'bin', 'python'),
    script: win ? P.join(venv, 'Scripts', 'clawtouch-mcp.exe') : P.join(venv, 'bin', 'clawtouch-mcp'),
    cfg: P.join(venv, 'pyvenv.cfg'),
    probe: P.join(root, 'probe'),
    marker: P.join(root, MARKER),
    lock: P.join(root, 'install.lock'),
    tmp: P.join(root, 'tmp'),
  }
}

/**
 * Compare versions the way it matters here (a PEP 440 subset): the numeric
 * release first; then a pre-release or dev build (0.6.1rc1, 0.6.1.dev1)
 * before the release, a post release after it. Returns -1, 0, 1.
 */
export function compareVersions(a, b) {
  const parse = (v) => {
    const m = /^\s*v?(\d+(?:\.\d+)*)(.*)$/i.exec(String(v))
    if (!m) return { release: [0], rank: 0 }
    const rest = m[2].toLowerCase()
    const rank = !rest || rest.startsWith('+') ? 0 : /^[.-]?post/.test(rest) ? 1 : -1
    return { release: m[1].split('.').map(Number), rank }
  }
  const x = parse(a)
  const y = parse(b)
  for (let i = 0; i < Math.max(x.release.length, y.release.length); i++) {
    const d = (x.release[i] ?? 0) - (y.release[i] ?? 0)
    if (d) return d < 0 ? -1 : 1
  }
  return Math.sign(x.rank - y.rank)
}

/**
 * The interpreters worth asking, most likely first. Absolute install
 * locations come before PATH: dsh inherited its PATH when it started, so a
 * Python installed since is found only where its installer puts it.
 *
 * Skipped on purpose: Windows' WindowsApps aliases (the Store stub can offer
 * to open the Store; the Python install manager's real runtimes are found in
 * its own folder instead), and on macOS /usr/bin/python3 unless the active
 * developer directory has a Python (otherwise running it pops up the Command
 * Line Tools installer on the person's screen). `developerDir` is what
 * `xcode-select -p` printed, or null.
 */
export function candidates({ platform = process.platform, env = process.env, exists = existsSync, list = safeList,
  developerDir = null } = {}) {
  const P = pathsFor(platform)
  const out = []
  const add = (command, args = []) => {
    if (!out.some((c) => c.command.toLowerCase() === command.toLowerCase() && c.args.join(' ') === args.join(' '))) {
      out.push({ command, args })
    }
  }
  const onPath = (names) => {
    const found = []
    for (const dir of String(env.PATH ?? env.Path ?? '').split(P.delimiter)) {
      if (!dir) continue
      for (const name of names) {
        const full = P.join(dir, name)
        if (exists(full)) found.push(full)
      }
    }
    return found
  }
  const newestFirst = (a, b) => compareVersions(b.replace(/\D+/g, '.').replace(/^\.+/, ''),
    a.replace(/\D+/g, '.').replace(/^\.+/, ''))
  if (platform === 'win32') {
    const root = env.SystemRoot || env.windir || 'C:\\Windows'
    const launcher = P.join(root, 'py.exe')
    if (exists(launcher)) add(launcher, ['-3'])
    const local = env.LOCALAPPDATA
    const versioned = (dir, prefix, file) => list(dir)
      .filter((n) => n.toLowerCase().startsWith(prefix))
      .sort(newestFirst)
      .map((n) => P.join(dir, n, file))
      .filter((p) => exists(p))
    if (local) {
      for (const p of versioned(P.join(local, 'Programs', 'Python'), 'python3', 'python.exe')) add(p)
      for (const p of versioned(P.join(local, 'Python'), 'pythoncore-3', 'python.exe')) add(p)
    }
    for (const base of [env.ProgramFiles, env['ProgramFiles(x86)']]) {
      if (base) for (const p of versioned(base, 'python3', 'python.exe')) add(p)
    }
    for (const p of onPath(['python.exe', 'python3.exe'])) {
      if (!/\\microsoft\\windowsapps\\/i.test(p)) add(p)
    }
  } else if (platform === 'darwin') {
    for (const p of ['/opt/homebrew/bin/python3', '/usr/local/bin/python3']) if (exists(p)) add(p)
    const fw = '/Library/Frameworks/Python.framework/Versions'
    for (const v of list(fw).filter((n) => /^3\.\d+$/.test(n)).sort((a, b) => compareVersions(b, a))) {
      const p = P.join(fw, v, 'bin', 'python3')
      if (exists(p)) add(p)
    }
    const realStub = Boolean(developerDir) && exists(P.join(developerDir, 'usr', 'bin', 'python3'))
    for (const p of onPath(['python3'])) {
      if (p === '/usr/bin/python3' && !realStub) continue
      add(p)
    }
  } else {
    for (const p of onPath(['python3', 'python'])) add(p)
  }
  return out
}

function safeList(dir) {
  try {
    return readdirSync(dir)
  } catch {
    return []
  }
}

/**
 * The child environment: the person's own, minus what could redirect an
 * install or change which Python runs (PIP_*, PYTHON*, VIRTUAL_ENV,
 * CONDA_PREFIX). Proxy and certificate settings stay.
 */
export function childEnv(env = process.env, platform = process.platform) {
  const out = {}
  for (const [k, v] of Object.entries(env)) {
    if (/^(PIP_|PYTHON)/i.test(k) || /^(VIRTUAL_ENV|CONDA_PREFIX|__PYVENV_LAUNCHER__)$/i.test(k)) continue
    out[k] = v
  }
  out.PIP_DISABLE_PIP_VERSION_CHECK = '1'
  out.PIP_NO_INPUT = '1'
  // `--isolated` skips the user's pip configuration but not the global file
  // or the venv's own; the null device as the config file skips them all
  // (pip compares it with Python's os.devnull, spelled this way).
  out.PIP_CONFIG_FILE = platform === 'win32' ? 'nul' : '/dev/null'
  return out
}

/** Hide what a line could carry that is nobody's business: credentials in a
 *  URL (`user:pass@`, `token@`) and token-like query parameters. Whole lines only. */
export function redact(line) {
  return String(line)
    // greedy to the last @ before the host: a password may contain @ itself
    .replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/]*@/gi, '$1***@')
    .replace(/([?&][^=&\s]*(?:token|key|secret|pass|auth|sig|credential|session)[^=&\s]*=)[^&\s]+/gi, '$1***')
    .slice(0, LINE_MAX)
}

/** Split a stream into whole lines before anything looks at them. */
export function lineSplitter(onLine) {
  let pending = ''
  return {
    push(text) {
      pending += text
      const parts = pending.split(/\r?\n/)
      pending = parts.pop()
      if (pending.length > 64 * 1024) pending = pending.slice(-64 * 1024) // a runaway line
      for (const line of parts) if (line.trim()) onLine(redact(line))
    },
    end() {
      if (pending.trim()) onLine(redact(pending))
      pending = ''
    },
  }
}

/** Kill a child and everything it started. */
function killTree(child, platform = process.platform) {
  if (!child?.pid) return
  const plain = () => { try { child.kill('SIGKILL') } catch { /* gone */ } }
  try {
    if (platform === 'win32') {
      // by full path: dsh desktop's PATH is not a shell's
      const taskkill = win32.join(process.env.SystemRoot || process.env.windir || 'C:\\Windows', 'System32', 'taskkill.exe')
      spawn(taskkill, ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
        .on('error', plain)
        .on('exit', (code) => { if (code !== 0) plain() })
    } else {
      process.kill(-child.pid, 'SIGKILL') // its own process group (spawned detached)
    }
  } catch {
    try { child.kill('SIGKILL') } catch { /* gone */ }
  }
}

/**
 * Run one child: no shell, hidden, deadline, whole-tree kill on deadline or
 * abort. Resolves {code, out, err, timedOut, cancelled}; never rejects.
 * `onLine` hears every whole output line, redacted.
 */
export function run(command, args, { cwd, timeoutMs = STEP_MS, onLine, signal, env } = {}) {
  return new Promise((done) => {
    if (signal?.aborted) {
      done({ code: null, out: '', err: '', timedOut: false, cancelled: true })
      return
    }
    let child
    try {
      child = spawn(command, args, { cwd, env: env ?? childEnv(), shell: false, windowsHide: true,
        detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (error) {
      done({ code: null, out: '', err: String(error?.message ?? error), timedOut: false, cancelled: false })
      return
    }
    let out = ''
    let err = ''
    let timedOut = false
    let cancelled = false
    const lines = onLine ? [lineSplitter(onLine), lineSplitter(onLine)] : null
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (t) => { if (out.length < 1 << 20) out += t; lines?.[0].push(t) })
    child.stderr.on('data', (t) => { if (err.length < 1 << 20) err += t; lines?.[1].push(t) })
    let settled = false
    let backstop
    const finish = (code) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      clearTimeout(backstop)
      signal?.removeEventListener('abort', onAbort)
      lines?.forEach((l) => l.end())
      done({ code, out, err, timedOut, cancelled })
    }
    // After a kill, 'close' waits for every holder of the output pipes; a
    // descendant that survived would hold them for ever. Give up on it then.
    const kill = () => {
      killTree(child)
      if (backstop === undefined) {
        backstop = setTimeout(() => finish(null), KILL_GRACE_MS)
        backstop.unref?.()
      }
    }
    const timer = setTimeout(() => { timedOut = true; kill() }, timeoutMs)
    timer.unref?.()
    const onAbort = () => { cancelled = true; kill() }
    signal?.addEventListener('abort', onAbort, { once: true })
    child.on('error', (error) => { err += String(error?.message ?? error) })
    child.on('close', (code) => finish(code))
  })
}

/** What screenshots and the window list need: mss everywhere, Quartz on macOS. */
const screenshotReady = (info, platform) => Boolean(info?.mss) && (platform !== 'darwin' || Boolean(info?.quartz))

const lastJson = (text) => {
  try {
    const info = JSON.parse(String(text).trim().split(/\r?\n/).pop())
    return info && typeof info === 'object' ? info : null
  } catch {
    return null
  }
}

/**
 * Ask each candidate what it is. Returns
 *   { pythons: [{id, command, executable, version, venv}],         // usable to install with
 *     found: [{path, version, python, screenshot}],                 // clawtouch-mcp already installed
 *     managed: {path, version} | null,                              // the plugin's own venv
 *     tooOld: [{executable, version}], root }
 * A python's `command` is the interpreter path the probe reported, never the
 * launcher that found it.
 */
export async function detect({ runner = run, platform = process.platform, env = process.env, exists = existsSync,
  list = safeList, home = dshHome(env), mkdir = mkdirSync } = {}) {
  const paths = managedPaths({ home, platform })
  // An empty directory of our own as the working directory: `-c` puts the
  // working directory on sys.path.
  try { mkdir(paths.probe, { recursive: true }) } catch { /* the probe still runs */ }
  const ask = async (command, args) => {
    const r = await runner(command, [...args, ...PY, '-c', PROBE], { cwd: paths.probe, timeoutMs: PROBE_MS })
    if (r.code !== 0) return null
    const info = lastJson(r.out)
    return Array.isArray(info?.version) && typeof info.executable === 'string' && info.executable ? info : null
  }
  let developerDir = null
  if (platform === 'darwin') {
    const r = await runner('xcode-select', ['-p'], { cwd: paths.probe, timeoutMs: PROBE_MS })
    if (r.code === 0 && r.out.trim()) developerDir = r.out.trim().split(/\r?\n/)[0]
  }
  const pythons = []
  const found = []
  const tooOld = []
  const seen = new Set()
  const key = (p) => (platform === 'win32' ? p.toLowerCase() : p)
  // asked four at a time: each probe has its own deadline, so the whole
  // detection stays well inside the page's wait however many there are
  const all = candidates({ platform, env, exists, list, developerDir })
  const answers = new Array(all.length)
  let next = 0
  await Promise.all(Array.from({ length: Math.min(4, all.length) }, async () => {
    while (next < all.length) {
      const i = next++
      answers[i] = await ask(all[i].command, all[i].args)
    }
  }))
  for (const info of answers) {
    if (!info || seen.has(key(info.executable))) continue
    seen.add(key(info.executable))
    if (key(info.executable).startsWith(key(paths.venv))) continue // our own venv, reported below
    const version = info.version.join('.')
    if (compareVersions(version, MIN_PYTHON.join('.')) < 0) {
      tooOld.push({ executable: info.executable, version })
      continue
    }
    pythons.push({ id: pythons.length, command: info.executable, executable: info.executable,
      version, venv: Boolean(info.venv) && !info.invenv })
    if (info.mcp && Array.isArray(info.scripts) && info.scripts.length) {
      found.push({ path: info.scripts[0], version: String(info.mcp), python: info.executable,
        screenshot: screenshotReady(info, platform) })
    }
  }
  let managed = null
  if (exists(paths.python) && exists(paths.script)) {
    const info = await ask(paths.python, [])
    managed = { path: paths.script, version: info?.mcp ? String(info.mcp) : null,
      screenshot: info ? screenshotReady(info, platform) : false }
  }
  return { pythons, found, managed, tooOld, root: paths.root }
}

/**
 * One install at a time per plugin instance, and one per dsh home across
 * instances (a lock file in the install folder: dsh desktop and a dsh in a
 * terminal share it). `start()` returns false when it cannot start; the page
 * polls `state()`. `close()` is for good: a disposed plugin starts nothing.
 */
export class Installer {
  constructor({ runner = run, home, platform = process.platform, fs = {}, timeoutMs = 8 * 60_000 } = {}) {
    this.runner = runner
    this.platform = platform
    this.paths = managedPaths({ home: home ?? dshHome(), platform })
    this.fs = {
      exists: existsSync, mkdir: mkdirSync, writeFile: writeFileSync, lstat: lstatSync, stat: statSync,
      open: openSync, write: writeSync, close: closeSync, unlink: unlinkSync, rm: rmSync, ...fs,
    }
    this.timeoutMs = timeoutMs
    this.job = null
    this.controller = null
    this.closed = false
  }

  get running() {
    return this.job !== null && this.job.phase !== 'done' && this.job.phase !== 'failed'
  }

  state() {
    if (!this.job) return null
    const { phase, lines, error, path, source, version } = this.job
    return { phase, lines: [...lines], error, path, source, version }
  }

  /** Stop a running job and refuse new ones: the plugin is going away. */
  close() {
    this.closed = true
    this.controller?.abort()
  }

  /**
   * @param {{command: string}} python  one of detect().pythons (the probed interpreter path)
   * @param {() => void} [onEnd]  called once when the job ends, either way
   */
  start(python, onEnd) {
    if (this.closed || this.running) return false
    this.controller = new AbortController()
    this.job = { phase: 'venv', lines: [], error: null, path: null, source: null, version: MCP_VERSION }
    this._run(python, this.controller.signal)
      .catch(() => { if (this.running) { this.job.phase = 'failed'; this.job.error = 'failed' } })
      .finally(() => onEnd?.())
    return true
  }

  _log(line) {
    const lines = this.job.lines
    lines.push(redact(line))
    if (lines.length > LOG_LINES) lines.splice(0, lines.length - LOG_LINES)
  }

  /** Take the install folder's lock, or say why not. */
  _lock() {
    const { open, write, close, stat, unlink } = this.fs
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const fd = open(this.paths.lock, 'wx')
        try {
          write(fd, JSON.stringify({ pid: process.pid, at: new Date().toISOString() }))
        } catch {
          // a lock that could not be written is still ours: do not leave it behind
          try { close(fd) } catch { /* closed */ }
          try { unlink(this.paths.lock) } catch { /* gone */ }
          return false
        }
        close(fd)
        return true
      } catch (error) {
        if (error?.code !== 'EEXIST') return false
        try {
          if (Date.now() - stat(this.paths.lock).mtimeMs < LOCK_STALE_MS) return false
          unlink(this.paths.lock) // left by an install that died; take it over
        } catch {
          return false
        }
      }
    }
    return false
  }

  _unlock() {
    try { this.fs.unlink(this.paths.lock) } catch { /* already gone */ }
  }

  async _run(python, signal) {
    const job = this.job
    const fail = (error) => { job.phase = 'failed'; job.error = error }
    const { exists, mkdir, writeFile } = this.fs
    try {
      mkdir(this.paths.root, { recursive: true })
      // marks the folder as ours: only then is its venv ours to rebuild
      if (!exists(this.paths.marker)) {
        if (exists(this.paths.venv)) return fail('not-ours')
        writeFile(this.paths.marker, 'Created by dsh-clawtouch for its clawtouch-mcp install.\n')
      }
    } catch {
      return fail('no-folder')
    }
    if (!this._lock()) return fail('locked')
    try {
      await this._install(python, signal, job)
    } finally {
      this._unlock()
    }
    if (signal.aborted && job.phase !== 'done') fail('cancelled')
  }

  async _install(python, signal, job) {
    const fail = (error) => { job.phase = 'failed'; job.error = error }
    const deadline = Date.now() + this.timeoutMs
    const left = () => deadline - Date.now()
    // pip's temporary files go in a folder of the install's own, emptied each time: the
    // system one can be unwritable (a sandbox, a locked-down account), and then Python falls
    // back to the working directory or pip waits on a lock it can never take.
    try {
      this.fs.rm(this.paths.tmp, { recursive: true, force: true })
      this.fs.mkdir(this.paths.tmp, { recursive: true })
    } catch { /* pip still has the system's */ }
    const env = { ...childEnv(process.env, this.platform), TEMP: this.paths.tmp, TMP: this.paths.tmp, TMPDIR: this.paths.tmp }
    const step = (command, args, opts = {}) => this.runner(command, args, {
      cwd: this.paths.root, env, signal,
      timeoutMs: Math.max(1000, Math.min(opts.capMs ?? STEP_MS, left())),
      onLine: opts.quiet ? undefined : (l) => this._log(l),
    })
    const stop = (r) => {
      if (r.cancelled || signal.aborted) { fail('cancelled'); return true }
      if (r.timedOut || left() <= 0) { fail('timeout'); return true }
      return false
    }
    // A link (or junction) as the venv would send pip, or --clear, somewhere else.
    if (this.fs.exists(this.paths.venv)) {
      try {
        if (this.fs.lstat(this.paths.venv).isSymbolicLink()) return fail('not-ours')
      } catch {
        return fail('not-ours')
      }
    }
    // 1. the venv: kept when it is a working venv with pip, rebuilt otherwise
    let healthy = false
    if (this.fs.exists(this.paths.cfg) && this.fs.exists(this.paths.python)) {
      const r = await step(this.paths.python, [...PY, '-c', PROBE], { quiet: true })
      if (stop(r)) return
      const info = r.code === 0 ? lastJson(r.out) : null
      healthy = Boolean(info?.invenv && info.pip)
    }
    if (!healthy) {
      this._log(`> python -m venv ${this.paths.venv}`)
      const args = [...PY, '-m', 'venv']
      if (this.fs.exists(this.paths.venv)) args.push('--clear')
      const r = await step(python.command, [...args, this.paths.venv])
      if (stop(r)) return
      if (r.code !== 0) return fail('venv')
    }
    // 2. pip, isolated from the person's pip configuration: PyPI, then the mirror once
    job.phase = 'pip'
    let installed = false
    for (const [i, [source, index]] of INDEXES.entries()) {
      const last = i === INDEXES.length - 1
      this._log(`> pip install ${MCP_SPEC} -i ${index}`)
      // -u: lines as they come, not when a pipe buffer fills. --no-cache-dir: no cache
      // folder elsewhere to write, or to wait on a lock in.
      const r = await step(this.paths.python, [...PY, '-u', '-m', 'pip', '--isolated', 'install',
        '--disable-pip-version-check', '--no-input', '--no-cache-dir', '--timeout', '15', '--retries', '1',
        '-i', index, MCP_SPEC], { capMs: last ? STEP_MS : PIP_TRY_MS })
      if (r.cancelled || signal.aborted) return fail('cancelled')
      if (r.code === 0 && !r.timedOut) {
        job.source = source
        installed = true
        break
      }
      // a source that does not answer in time is a reason to try the next, not to give up
      if (r.timedOut) this._log(`> no answer from ${index} in time`)
      if (left() <= 20_000) return fail('timeout')
    }
    if (!installed) return fail('pip')
    // 3. what is installed is what was asked for, with what screenshots need, and its program is there
    job.phase = 'verify'
    const r = await step(this.paths.python, [...PY, '-c', PROBE], { quiet: true })
    if (stop(r)) return
    const info = r.code === 0 ? lastJson(r.out) : null
    const ok = info?.mcp === MCP_VERSION && screenshotReady(info, this.platform) && this.fs.exists(this.paths.script)
    if (!ok) return fail('verify')
    job.path = this.paths.script
    job.phase = 'done'
  }
}
