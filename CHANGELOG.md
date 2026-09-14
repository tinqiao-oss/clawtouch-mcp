# Changelog

All notable changes to `clawtouch-mcp` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions adhere to [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed — dsh plugin (`dsh-clawtouch` 0.1.2): results that reported input which never happened, and text that stopped halfway

Three ways the plugin could tell the agent something was pressed when it
was not, or leave it guessing what was:

- **`mock: true` reported clicks.** `clawtouch-mcp --mock` has no device and
  answers every action with success — `hid.click` says `clicked: true` —
  and the plugin relayed that as "clicked (x, y)". The README describes
  `mock` as running without hardware, so it is what someone without a board
  tries first. The plugin now settles whether the server is a mock before
  it sends anything — from the `--mock` it started the server with, or else
  from the server's own `device.info`, asked once per session. Only a real
  answer counts (an object with the `info` block the server always sends);
  when none can be read, the tool sends nothing and says that whether the
  input would reach a device is unknown, and asks again next time instead
  of remembering a guess. A mock still receives the action, so the path is
  exercised, and the result reads "would click … — nothing was pressed:
  clawtouch-mcp is running with --mock". The same goes for sequences,
  typing, keys and scrolling; a mock server is not asked to raise a window
  either. Settling it first matters: a question asked after the input went
  out could fail and report a failure for input that happened, which
  invites the agent to send it twice.
- **`dryRun: true` could press a title bar.** It is documented as locating
  and reporting "without pressing anything", but bringing a background
  window forward is a real click on its caption (in a browser that can open
  a tab), and it happened before the dry-run check. A dry run now works
  with the window as it is, and refuses a covered one with the reason no
  raise was attempted. Every dry-run result — clicks, typing, keys,
  scrolling — says "nothing was pressed: dryRun is on". (Text that cannot
  be typed is refused in a dry run too, as it would be for real.)
- **Non-ASCII text was typed halfway.** The device types one key per
  character on a US layout; the first character without a key (Chinese, an
  emoji, a curly quote, "é") stops the firmware after everything before it
  was typed, and the server sends the text in 32-character chunks, going on
  to the next after a failed one. `computer_type` now checks the whole text
  first and refuses — before its `target` click, so nothing on screen
  changes — naming the characters to rewrite. The tool description says
  "plain ASCII only" up front. When the server leaves out control
  characters (on purpose: a newline must not submit a chat draft), the
  result now says how many and to send Enter or Tab with `computer_key`,
  instead of a bare "typed 9 characters" for a 10-character request.

The skill and the `computer_click` description now describe the device
plainly — a USB HID mouse and keyboard acting on this machine's screen,
with no undo — and the skill adds what the agent was missing: text is typed
on a US layout (and a Chinese input method can turn typed letters into
candidates), and "would click" / "nothing was pressed" means no input
happened. The README's hardware paragraph now says what the board does in
this plugin and how to check the locating without one: `dryRun` needs no
board (verified with the board's ports held by another process: the server
falls back to its no-device state and still lists windows and captures).

The typing, key and scroll tools moved into `lib/actions.js` so they are
tested without a host. 26 new tests in `test.js`: the typing check, mock
and dry-run reporting for every input tool, no raise click in either, the
order (mock status settled before any input, including a target click),
and an unreadable, wrongly shaped, failing or concurrent `device.info`.
`smoke.js` checks the typing refusal through the host, and that the skill
and tool descriptions keep to what the plugin does.

### Fixed — dsh plugin (`dsh-clawtouch` 0.1.2): Alt+Tab ended a run, and the quit guard had a way around it

A real keystroke goes wherever focus is. In a head-to-head run on Windows
(2026-09-13), the agent — looking for a window that was not there — sent
`computer_key` Alt+Tab. It went out through the device, focus moved to the
editor behind the task window, and the rest of the run could not get back:
every later key and every "type into the focused field" would have landed
in the editor. Nothing refused it, because the guard only knew three quit
combos.

The guard now also refuses **window-switching** combos by default. On
Windows and Linux desktops: Alt+Tab (with or without Shift), Alt+Esc,
Ctrl+Esc, Ctrl+Alt+Del, and every Windows/Super-key combination (the shell
owns all of them — Start, show desktop, run, lock, snap). On macOS: Cmd+Tab,
Cmd+Space, Cmd+\`, Cmd+H, Cmd+M, Cmd+Option+Esc, Ctrl+arrows and
Ctrl+F2/F3, while ordinary Cmd shortcuts (Cmd+C, Cmd+S) still go through.
The refusal tells the agent the way that does work: on Windows, name the
window in `computer_click`, which raises it by its title bar; elsewhere,
click a visible part of it. `allowFocusSwitchCombos: true` turns this class
off for a setup where the machine being driven is not the one running dsh.

The quit class is now spelled per platform too — Cmd+Q / Cmd+W on macOS;
Alt+F4, Ctrl+W and Ctrl+F4 on Windows and Linux (Ctrl+W is the same
close-this gesture as Cmd+W, and closes a browser tab showing the dsh web
UI). A Windows-key combination is no longer described as "Cmd+Q": Win+Q is
the shell's search and is refused as a focus switch. `allowQuitCombos`
switches only the quit class.

Two older holes, found while testing this and in review of it:
`clawtouch-mcp` reads shorthand in `key` (`"alt+f4"` is F4 held with Alt),
but the guard compared `key` with `"f4"`, so `computer_key({ key: "alt+f4" })`
walked past the quit check; and the server strips names with Python's
`str.strip()`, which removes U+001C–U+001F and U+0085 where JavaScript's
`trim()` does not, so `key: "tab"` held with Alt was pressed as Alt+Tab
while the guard saw an unknown key. Both classes now read a combination the
way the server will — shorthand split, the union of both whitespace sets
stripped, a numeric key read as its digits — so the guard sees at least
what the server presses. The rules live in a pure `lib/keyguard.js`, so they
are unit-tested without a host (11 new tests in `test.js`, which CI runs,
including each reproduced bypass; the host guard table in `smoke.js` covers
the shorthand and each opt-out on its own).

### Fixed — dsh plugin (`dsh-clawtouch` 0.1.1): one click in six failed on a missing brace

With `qwen-vl-max` as the eye, a single-target `computer_click` regularly
came back as

    {"markers":{"tl":[14,15],"br":[298,526],"target":{"found":true,"point":[43,350],…}}

— the `markers` object never closed. The plugin parses strictly on
purpose, so each of these was a failed click. Measured over 165
single-target calls on four ordinary Windows windows: **15.8%** of replies
had exactly this shape (**44%** on a small Calculator window). The content
was right every time — with the brace restored, all of those points landed
inside their targets — so the failure was pure loss. Batch replies
(`computer_click_sequence`) almost never do it.

`parseAnswer` now repairs that one shape and nothing else. Only the whole
reply is repaired (a leading fence is fine, prose around it is not — cutting
prose away can also cut away a second answer). It must open with `markers`,
holding exactly `tl` and `br`, once each, each a pair of numbers, running
straight into `"target"`/`"targets"`. After the one brace is added the
result must parse, carry exactly one of `target`/`targets` at the top, and
repeat no key in any object: `JSON.parse` silently keeps the last duplicate,
which would let the repair choose between two answers (`"found":false` then
`"found":true`, or a second key spelled `"target"`). The repair moves no
value, so it cannot make the model point anywhere it did not. Everything
else malformed still fails loudly, and every shape two review rounds found
is a negative test; removing any one guard fails the suite. When the repair
fires, `parseAnswer` returns `repaired: true` and the locator logs it, so
the rate stays checkable against real traffic.

Re-measured with the final repair: it fired on 25 of 189 single-target
calls and all 25 landed inside their targets; single-target hit rate went
from 80.6% to 95.2%, wrong clicks were 4.8% against 3.6% before (8 vs 6 of
165 — run-to-run noise; the repair moves no coordinate), and every one of
24 absent-target probes was still refused. Two "one-line" alternatives were
measured first and rejected: a numeric JSON example in the prompt made the
model copy it (wrong clicks rose from 3.6% to 18%), and
`response_format: json_object` turned the broken replies into "not found"
instead of answers.

## [0.5.1] — 2026-08-28 — the frontmost window is measured, not guessed

### Fixed — macOS reported a `foreground` window it had never measured

`screen.windows` on macOS flagged whichever window CGWindowList returned
first. That order answers a real question — topmost first — but not the
one the field claims: the list contains every layer-0 window an
application owns, and the service windows sort ahead of the one the user
is looking at. Measured on macOS 26: VS Code's first entry is a 1512x32
strip, and the editor window it belongs to sorts second, so the flag was
on the strip.

That is worse here than it would be anywhere else. Occlusion and input
state are deliberately not reported on macOS, which leaves `foreground`
as the only window fact a caller can still act on — so it has to be one
that was actually measured. Half of it now is:
`NSWorkspace.frontmostApplication()` supplies a pid, and that is a real
query — the flag can no longer land on some other application's window,
which is what index 0 allowed. Which of that application's windows gets
it stays a heuristic and says so in the code: `kCGWindowName` is
documented as optional, so preferring an entry that has one is a tiebreak
that separated the strips from the real window in every case measured,
not a classification to rely on. Only on-screen entries are eligible,
because `include_offscreen=True` switches to a listing whose order
carries no front-most meaning and which spans other Spaces.

Those are two different outcomes and they are not reported the same way.
When the frontmost application simply has no eligible window, nothing is
flagged and the field stays `false` — the OS was asked, and "none of
these" is what it said. When the question could not be answered at all,
the field is **dropped**, because `false` there would assert something
nobody checked. Getting that boundary right needed the measurements
above; guessing it would have produced exactly the class of bug this
change exists to remove.

That new "nothing is flagged" state needed the plugin to stop overselling
its own fallback: `resolveRegion` still captures the first listed window
when no window reported itself as foreground, which is the useful thing
to do, but it no longer *calls* it the foreground window in the string it
hands the agent — that would have re-told the exact guess the server
stopped making, one layer up and unverifiable.

Downstream this was not cosmetic: the plugin picks its capture region
from the flag whenever no window is named, so an untargeted
`computer_click` cropped to a 32-pixel strip — and on a crop that short
the two calibration markers sit 4 px apart vertically, which surfaces as
a calibration failure and reads like a HiDPI bug.

`AppKit` adds no new requirement: pyobjc-framework-Quartz, which the
`[window]` extra already installs, depends on pyobjc-framework-Cocoa.
Windows is untouched — it has always read `GetForegroundWindow()`.

### Fixed — three places that already had the answer and threw it away

The `foreground` work above produced a trustworthy answer, and three
paths went on ignoring it. They are one shape, so they are described
together.

**`find_window` reselected the service strip.** An untitled window is
listed under its application's *name*, so VS Code's 1512x32 strip matches
`find_window("Code")` **exactly** while the editor window the caller
means matches only loosely — and exactness was ranked first, so the strip
won. That is the same 32-pixel crop this release stopped producing,
reached by the by-title road instead of the by-foreground one. The
function's own docstring had promised "front-most window wins ties" all
along; Windows sorts its list foreground-first, so it was true there by
accident, and macOS returns CGWindowList order, where it was not. It is
now implemented: front-most first, exactness second. The trade that
accepts: asked for "Calc" while a foreground window matches loosely and a
background one matches exactly, the foreground one wins — the answer a
person would give to "the Calc window", and the alternative puts the
strip back.

**A raise that the re-read said had not happened was treated as success.**
`ensureReachable` clicks a window *on purpose* to bring it forward, then
re-reads it — and checked only that something came back, that it was the
same window, and that it was not covered. A reply of `foreground: false`
went through. That is a failed action: some applications swallow the
first click as activation, so the next click, aimed at a target, does
nothing. Only an explicit `false` refuses; an absent answer does not,
for the same reason the occlusion guard lets an unmeasured window
through.

**`pid` did not prove identity.** The re-read asks by title, which is
mutable and not unique, so the reply is checked against what was asked
for — but two windows of the *same application* share a pid, and a
sibling passed. Every coordinate after that pointed at the wrong window.
A rectangle completes it: raising does not move a window, so a changed
rect is a different one. Both fields are compared only when both sides
carry them.

Identity and raise-success now each have one function answering them
(`sameWindow`, `raiseTookEffect`), because the pattern here was three
partial answers in three places.

### Fixed — the plugin's own test suite had stopped running on CI

`node test.js` is the one Node job the public CI runs, and it runs it on a
clean checkout with no `npm install` — which works only because the suite
imports nothing outside `lib/`. A test added earlier in this change
imported `index.js` to reach the `computer_windows` renderer, and
`index.js` imports `@deepseek-ai/dsh-tools`; from then on the job could
only have passed on a machine that happened to have that package lying
around. Locally it did. On a clean tree it is `ERR_MODULE_NOT_FOUND`.

The renderer moved to `lib/locator.js`, next to `unmeasuredNote`, whose
rule it applies to the tool's output shape. That is where it belonged
anyway — the two had drifted into saying different things about the same
window — and it puts it below the dependency line, so the suite is
dependency-free again. Verified against a checkout with no
`node_modules`.

Two smaller things in the same suite: the genuine-miss fixture claimed
`isError: false` where the real server sends `isError: true` alongside
`available`, which would have hidden a guard that keyed off the wrong
field; and the check that pins the count quoted in `TESTING-macos.md` was
itself a test, so it only saw the tests declared above it and a test
appended below would have left the doc stale and the suite green.

### Fixed — the plugin turned two kinds of "could not look" into answers

Making the server honest about an unreadable window list exposed two
places where the plugin turned that failure back into a measurement.

**A lookup that failed was reported as absence.** `resolveRegion`'s
titled query checked only whether a window came back, so an errored reply
became `no visible window matching "X"` — and because the titles were
then taken from the *previous* listing, the message could name the very
window it claimed was not there: `no visible window matching "Calc".
Visible windows: "Calc"`. The server's own explanation was dropped on the
floor, so the agent was told to retry a lookup that could not succeed.
The two are distinguishable — the server sends `available` only on a
genuine miss — and that is now the test. The same rule was already
applied a hundred lines further down, to the post-raise re-read.

**A named window was answered by widening to the whole desktop.** When
the listing could not be read at all, `resolveRegion` fell back to a
full-screen capture even when the caller had named a window. Capturing
everything answers a different question, and the model then locates in UI
nobody mentioned. The fallback is right when no window was named and
wrong when one was, so it now refuses in that case. This is older than
the change above — but it used to be unreachable on the macOS path,
because an unreadable listing arrived as "no visible window matching X"
and was refused. Making the server truthful routed a new failure into it,
so it had to be closed in the same breath.

### Fixed — an unreachable window server was reported as an empty desktop

`CGWindowListCopyWindowInfo` has two different empty answers and Apple
separates them: no matching windows returns an empty array, while NULL
means it could not answer at all. Apple documents two causes for the
NULL, and they need different things from the operator — the caller is
not running within a Quartz GUI session (started over SSH, or from a
launchd daemon rather than a login session), or the window server is
disabled. `or []` collapsed that into the first case, so `screen.windows`
answered `{"windows": [], "count": 0}`, and a titled query answered "no
visible window matching X" about an application that was running the
whole time.

It now raises `WindowInfoUnavailable`, the same way a missing pyobjc does,
with a message that separates it from both neighbours — this is not "your
platform is unsupported" and not "nothing is open". The second listing
added above already treated NULL as failure; this is the same rule at the
front door, where it matters more, because an empty list reads as an
answer and an exception does not.

Not reproduced on hardware: forcing a session without window-server access
needs the machine's SSH configuration changed. The contract is Apple's.

### Fixed — macOS answered `minimized: false` without ever asking

`include_offscreen` exists to admit minimized windows — that is what its
own description promises — and every macOS entry came back asserting
`minimized: false`. The flag's whole purpose, denied by its own results.
Windows measures this properly; macOS is never asked, so the key is now
absent there, like the other answers this platform does not have.

Deliberately not inferred from `kCGWindowIsOnscreen` either: off-screen on
macOS also covers "on another Space", so reading it as "minimized" would
be the same guess wearing a different hat.

### Fixed — `foreground: false` was itself an unmeasured claim

Fixing "reported a window it never measured" left the other half in
place: every entry was pre-written `foreground: false`, so when the
frontmost query could not be made at all, callers were handed a plain
assertion — *this window is not in front* — where the truth was that
nobody had looked. That is the same substitution the missing guards
refuse to make, and it was inconsistent with them: `visible_fraction` and
`enabled` say "not measured" by being **absent**.

They now all say it the same way. `_frontmost_pid_darwin` answers three
things instead of two — a pid, a real 0, or `None` for "could not ask" —
and only the last drops the key. Which cases fall on which side is not
guessable, so it was measured on macOS 26 rather than assumed: with the
screen **locked** the query answers loginwindow's pid, not nil; an
application whose windows are all minimised, or all on another Space,
still answers with its own pid. Every one of those is a real answer, so
`false` there is a true statement and stays. `None` is left for the query
itself failing — which, since pyobjc-framework-Quartz *requires*
pyobjc-framework-Cocoa, is close to unreachable in practice.

The plugin follows: `foreground` is optional in the `computer_windows`
schema, the `Boolean(w.foreground)` coercion that turned missing into
`false` is gone, and `unmeasuredNote` names it alongside the others, so
an answer computed without it says `which is frontmost unmeasured here`.

One Windows-visible consequence, since it is a shared path:
`GetForegroundWindow()` may legitimately return NULL in the instant a
window is losing activation. Every entry is then `false` — a correct
answer there, since Windows really is saying "no foreground window", not
"could not look" — and the plugin's region source reads `first listed
window "…" (nothing reported itself as foreground)` instead of
`foreground window "…"`. The region and every HID action are unchanged;
only the sentence differs.

### Fixed — `include_offscreen=True` was still guessing which window is in front

Restricting the pick to on-screen entries was not enough. That mode asks
CGWindowList for everything, and dropping the off-screen entries from
such a listing does not restore the z-order of the ones that remain — the
order simply does not carry front-most meaning there, as the module's own
comment already said. With two or more on-screen windows owned by the
frontmost application, the flag was a coin flip.

The pick now always comes from an on-screen listing, and that mode pays
for a second small query to get one rather than reusing the list it
already has. Matching is by window number, which is not a race-free story and should
not be told as one: if the frontmost application opened a window between
the two snapshots, the new id is not in the map, the walk continues, and
an OLDER window of that application gets flagged. A narrow window, and
this level was already declared best effort — but "can only fail to flag"
would be the wrong thing for the next reader to believe, so the code says
so where it happens. If that second query returns NULL (its documented failure, which is
not the same as a successful empty listing) the field is dropped instead
of leaving `false` behind. The pick also walks the frontmost
application's windows in order and takes the first one the reported list
actually contains, because the two listings are filtered differently: a
zero-sized window can be worth ordering by and not worth reporting, and
picking one id and hoping meant nothing got flagged while a good window
sat right behind it. The default path issues no extra query: its listing
is already on-screen-only.

### Fixed — the out-of-range hint gave advice that cannot work

A coordinate outside the addressable screen announces itself rather than
being clicked silently (0.5.0), and the note told the caller to widen
`--screen` to cover the whole virtual desktop. That is the right answer
for a display to the right of or below the primary one, and no answer at
all for a display to the left or above: `--screen` carries a size and no
origin, so the addressable area always starts at `(0, 0)` and no `WxH`
admits a negative coordinate. Measured on macOS with the second display
moved to origin `(-1920, 0)`: `--screen 3432x1200` is the *size* of the
whole virtual desktop, yet it still addresses only `[0,3432)x[0,1200)`,
and every point on that display clamps to `x=0`. A caller following the
hint does exactly what they already did, and the second identical failure
reads as a broken tool rather than an unreachable point.

The two faults are now answered independently, because a point can be
both — negative on one axis and past an edge on the other — and
suppressing either half leaves the caller stuck on it. The negative half
also says to widen `--screen` afterwards: moving the display right of the
primary turns the coordinate positive and lands it past bounds that were
only ever the primary monitor, so rearranging alone would just move the
failure. And it stops short of asserting *why* the coordinate is
negative — a display placed left or above is the usual cause, not
something this layer measured.

A message change, not a behaviour change: the clamping, the refusals and
what is sent are all what they were, and the same split is applied at the
four places that said it (`_clamp_note`, and the plugin's click refusal,
multi-target refusal and raise-point warning). Not macOS-specific — a
Windows secondary monitor placed left of the primary has the same
negative coordinates. Also not the only physical route to such a display:
`relative: true` skips clamping altogether. That is deliberately not
offered as a remedy, since a relative delta cannot be aimed at a point
this layer located in absolute space.

### Fixed — `computer_find` expressed "not found" as a pair of integers

`found: false` came back with `x: -1, y: -1`, because the schema made the
coordinates required and something had to fill them. A specific pair of
integers standing for "no answer" is the same substitution this plugin
refuses everywhere else; the fields are optional now and simply absent.

### Documentation

- `adapters/dsh/plugin/TESTING-macos.md` rewritten against a real macOS
  26.5.2 pass (Retina primary + second display, HID hardware, vision key).
  Corrections that mattered: the install line asked for `[screenshot]`,
  which does not contain pyobjc, so the very next step of the same
  checklist could not pass — it needs `[screenshot,window]`; the commands
  used `python`, which a stock Mac does not have; `clawtouch-mcp
  --list-ports` was never a flag; and the document promised that a
  covered window "says so and stops", which macOS cannot do — the
  occlusion guard is gated on a number this platform never supplies, so
  the window is captured and clicked as if it were on top, with only the
  `(… unmeasured here)` suffix to say so.
- Added what a mac tester actually trips over: Spaces (the window list
  covers the active Space only, so a full-screen editor hides everything
  else and the honest error reads like the app is closed), localised
  window names (`--window Calculator` finds nothing on a Chinese-language
  Mac), the hardware-without-a-key tier (`probe.js --move-test`), what a
  30-second failure means, and the second-display refusal.
- The out-of-range hint also blamed the wrong thing when `--screen` was
  given explicitly: it said the value "defaults to the PRIMARY monitor",
  which is false once an operator has passed one, and a hint that is
  wrong about the cause is a hint that stops being read. It now names
  whichever the bounds actually came from.
- Both READMEs asked for `[screenshot]` where macOS also needs
  `[window]`. The plugin's README is the one that matters most — it ships
  to npm, `TESTING-macos.md` does not — and the plugin crops to a window
  before it looks at anything, so without pyobjc a call that names a
  window is refused and one that does not widens to the whole desktop.
- `server.py`: the convergence comment claimed accuracy is independent of
  screen size. Measured on a two-display Mac, the same window and targets
  settle 1 px out under `--screen 1512x982` and 4 px out under
  `--screen 3432x1200`, 6 interleaved runs of 6. Both are legitimate early
  exits inside `MOVE_TOLERANCE`, so the algorithm is unchanged and the
  note now records the observation without claiming the declared size
  caused it — the starting cursor and the carried-over gain estimate were
  not controlled. It does point at the one screen-size-dependent step in
  that path, for whoever chases it.


## [0.5.0] — 2026-08-24 — window geometry · calibration markers · pointer-gain convergence · the dsh plugin

### Added — windows are brought forward by clicking them, not by an API

A window that is not in front cannot be worked with: a capture of its
rectangle is a capture of whatever covers it, and some applications
swallow the first click as an activation. Until now the plugin refused and
told the agent to deal with it.

It now raises the window itself, **by clicking its title bar with the real
mouse** — the same thing a person does, and consistent with a project
whose whole point is that input is physical rather than injected.
`SetForegroundWindow` would have been one line, and is exactly the kind of
software-injected control this package exists to avoid; it is also
unreliable, as Windows' foreground lock refused it several times while
this was being tested.

`screen.windows` supplies the point as `raise_point`, and choosing it is
the whole trick. "The top strip is the title bar" is not safe — in a
browser that strip is the tabs, and a raise would open one. The point is
one the application itself reports as a drag area (`WM_NCHITTEST`
answering `HTCAPTION`) **and** that `WindowFromPoint` confirms is actually
on top.

That drag-area answer turned out to be necessary but not sufficient, and
it took a real browser to find out: Chrome reports its "new tab" button as
a drag area too, so picking the leftmost qualifying point opened a tab
instead of raising the window. The caption is therefore scanned from the
**right**, where the space just inside the window controls is drag area
and nothing else; the controls are excluded by their own hit-test answers
rather than by guessing how wide they are. Measured on one window: the old
rule chose x=7460, the "new tab" button; the new one chooses x=7528, the
empty strip, and the title is unchanged after the click. No
qualifying point means no raise: a full-screen app with no caption, or a
window buried completely, is reported rather than guessed at.

A modal-disabled window is still refused rather than raised — raising it
changes nothing, it discards clicks either way — and the result is
re-read after the click rather than assumed, because the click may have
raised something else.

Measured end to end, calculator starting in the background, agent told
only "it may not be in front": **17 seconds** for `7 + 8 =`, raise
included. The same shape of task took 37s before this and 58s before
`computer_click_sequence`.


### Added — `computer_click_sequence`: several clicks from one look

Locating is the slow part, and so is the agent's own turn between calls.
Clicking four calculator keys used to mean four screenshots, four vision
calls and four agent round trips for a screen that never changed in
between. This does it with one of each.

Measured on the same task (press `5 × 6 =`), same machine, same model:

| | model round trips | model time | wall clock |
|---|---|---|---|
| one `computer_click` per key | 23 | 31.1s | ~60s |
| one `computer_click_sequence` | **11** | **19.8s** | **~37s** |

Accuracy is unchanged: four targets in one vision call landed 2.3-5.6px
from truth, the same four asked separately landed 3.0-5.3px. Asking for
more does not make the answers worse; it just makes fewer of them.

The constraint is real and the tool description says so plainly: every
target must be visible **at the same time** and clicking one must not move
the others. Calculator keys qualify; opening a conversation does not.
Nothing is clicked unless every target was found — a half-finished
sequence is harder to recover from than one that never started — and the
clicks go out as a single `hid.batch`, which already paces discrete clicks
apart so the OS cannot coalesce them.

Everything here exists to make one thing work: a vision model pointing at a
UI element, and the cursor actually landing on it.

### Added — `screen.windows`

- Lists visible top-level windows with their titles and screen rectangles,
  so a capture can be cropped to **one window** instead of the whole
  desktop. Windows (ctypes/`user32` + DWM extended frame bounds, which
  excludes the invisible ~7px resize border `GetWindowRect` includes) and
  macOS (`Quartz.CGWindowListCopyWindowInfo`, needs pyobjc); a clear
  "unsupported" answer elsewhere. Shell-owned windows (`Progman`,
  `WorkerW`, the taskbar) and cloaked UWP ghost windows are filtered out —
  the desktop itself is a visible, titled, full-virtual-screen window and
  would otherwise be the widest entry in the list.
- Gated by the existing `--allow-screenshot`: window titles are the same
  order of disclosure as the pixels showing them, and a second flag would
  only be a second thing to forget.
- No new dependency. Rectangles are in the same coordinate space as
  `hid.click` and `hid.screenshot`'s `region`.

### Added — `hid.screenshot`: `max_width` and `markers`

- `max_width` bounds the returned image width (aspect preserved), applied
  after every other resize policy. Vision models rescale their input to an
  internal budget anyway; sending an ultrawide desktop whole only means the
  model discards the small-text detail a click depends on. Measured on a
  5120×1440 host: the full desktop located **0 of 6** targets, the same
  targets in a window cropped to ≤1920 located **6 of 6**.
- `markers: true` stamps two markers (red square, yellow ring, white centre
  dot) near the top-left and bottom-right corners **after** every resize,
  and reports their exact centres in the metadata. Asking a vision model
  for both marker centres alongside the target measures the model's own
  unreported internal rescale in the same call — two points per axis, so
  the fit absorbs a constant offset that a single-point ratio folds into
  the scale. Unlike using a known UI element as the anchor, this works on
  surfaces with no accessibility tree at all, which are exactly the
  surfaces that need visual clicking.
- Metadata gains `capture_rect` and `image_scale`, which together map an
  image point back to a clickable screen point across region crops,
  `max_width` and Retina captures without the caller knowing which applied.

### Fixed — absolute moves now converge on hosts with strong pointer acceleration

- The converge loop commanded the raw residual delta, which assumes the OS
  moves the cursor about as far as it is told (macOS amplifies ~1.1×). On
  Windows with **"Enhanced pointer precision" — the shipped default** — a
  large delta is amplified ~2.5×: measured on real hardware, commanding
  127px moved the cursor 319, commanding −1000 moved −2516. At that gain
  the loop does not decay, it oscillates; ten passes later a long move was
  still hundreds of pixels off and returned `ok: false`. Short hops
  converged fine (the ballistic curve is near 1× for small deltas), which
  is why this stayed invisible until a click had to cross a wide desktop.
- The loop now divides each commanded delta by a gain measured from the
  previous pass. No per-magnitude ballistics table and no larger iteration
  budget — it self-tunes to whatever the host does, in both directions
  (a host that damps input converges too), and costs two floats of state.
  Edge-clipped passes are excluded from the estimate: a move stopped by
  the screen edge travelled less than the OS would have moved it, and
  folding that in teaches the loop to overshoot harder.
- Verified on real hardware across a 7680×1440 two-monitor desktop:
  worst residual **328px → 5px**.

### Fixed — a clamped coordinate no longer passes silently

- `hid.click` / `hid.move` clamp to `--screen`, which defaults to the
  **primary** monitor. A clamped move still converged and still reported
  success — at a point the caller never asked for, which on a
  multi-monitor desktop is every click on the second screen.
- Results now carry `clamped: true`, `requested_x` / `requested_y` and a
  hint naming the fix. Deliberately **not** a failure: `ok` stays whatever
  the converge decided, because flipping it would also suppress the click
  (the click gate keys off `ok is False`) and turn a long-standing
  off-by-one at the screen edge into no click at all. Policy about whether
  a clamped target is acceptable belongs to the caller; this layer's job
  is to stop it being invisible.

### Changed

- `hid.screenshot`'s `region` is clamped to the monitor holding the
  region's centre rather than always to the primary monitor. A window on a
  second display is a legitimate target — that is what `screen.windows`
  hands back — and clamping it to primary silently returned pixels from
  the wrong screen. Still one monitor, never the union: capturing the
  whole virtual desktop is what the clamp exists to prevent.

### Added — `screen.windows` reports how much of a window is on top

Capturing a window's rectangle captures whatever is **in front of** that
rectangle, which is not the same thing as the window. Found the hard way:
a calculator sitting behind an editor was captured by its own rect, and a
vision model asked to find its "8" key answered *"there is no 8 key here,
this is a file explorer listing .env.deploy, feishu.json, ClawTouch"* —
completely correct, completely useless, and indistinguishable downstream
from a genuine miss. Four rounds of prompt tuning and an
upscale-the-image experiment were spent before anyone looked at the
picture.

Each window now carries `visible_fraction`: the share of a sampled grid
inside its rectangle that the OS says belongs to *it*. 1.0 is unobstructed;
0.0 means a capture of that area would be a capture of something else.
Windows-only for now (`WindowFromPoint` + `GetAncestor`, no new
dependency), absent elsewhere — and absent means "not measured", never
"fine".

### Fixed — the plugin kept the host process alive after it had finished

A one-shot `dsh` run that used a `computer_*` tool produced its answer in
about six seconds and then sat there, done, until something killed it two
minutes later. It reads as "this thing is unusably slow" and is nothing of
the kind: a timestamping proxy in front of the model showed every request
completed by t=6.3s and no further work of any sort.

The cause was here. A piped child process and each of its three stdio
streams hold a libuv handle that keeps Node's event loop alive, so the
`clawtouch-mcp` subprocess this plugin owns kept the whole host running
for as long as the device stayed connected. Handles are now released while
the client is idle and re-taken while a request is in flight, so the loop
can never exit mid-call.

Measured on the same task, same machine: **≥120s → 5.2s**. A four-click
multi-step task ("press 5 × 6 = on the calculator") went from minutes to
**58 seconds**, of which the plugin's own share is about 10 seconds — the
rest is the agent model deciding what to click next.

For the record, since it was the first suspect and was wrong: at
`reasoningEffort: low` the agent answers a no-tool question in 4.3s versus
40s at `high`, but with this bug fixed both settings finish the
tool-calling task in 5.2s. The reasoning effort was never the problem.

### Added — `screen.windows` reports whether a window accepts input at all

Visible and reachable are different facts, and only one of them is about
pixels. A window can be entirely unobstructed, foreground, screenshotting
perfectly — and discard every click, because a modal dialog somewhere
else has disabled it. A physical mouse cannot click it either.

Found by spending half an hour on a WeChat window that would not respond:
the coordinates were verified correct, the timing variants all failed, a
known-good control target failed too, the same click worked on another
application, and the device reported every move landed and every click
ACKed. All true, all useless. `IsWindowEnabled` would have said so in one
call — there was a hidden dialog waiting for input.

Each window now carries `enabled`. The plugin checks it **before** the
occlusion check, because a disabled window is usually unobstructed and
would otherwise sail straight through.

### Fixed — an adversarial review of the above, before any of it shipped

A second model was pointed at the diff with the project's invariants and
told to refute the reasoning rather than agree with it. Everything it
found was in one category: **a wrong answer that reported success.** That
is the only failure this design cannot absorb, because the agent has no
independent way to notice.

- **A cross-move gain that could strand a short move.** Carrying the
  estimate between moves saves an overshoot, but with a stale gain of 2.5
  and a target 24px away the commanded delta is 10px — under the sampling
  floor, so the estimate can never be re-measured while each pass creeps
  2px. Ten passes later it has gone nowhere: strictly worse than never
  persisting the gain. An unmeasurable pass is now corrected by what it
  did — a residual that grew raises the estimate, a residual that barely
  shrank walks it back toward 1.0, which makes the next command large
  enough to measure.
- **The boundary test read "beyond the edge" as "clipped by the edge".**
  A cursor pinned *at* the edge really was clipped and its ratio
  understates the gain; a position *past* it was clipped by nothing. The
  test is now equality, not a range — as a range it discarded every
  sample on hosts that don't clamp, leaving the loop unable to learn at
  all.
- Deliberately **not** fixed: a cursor stopped by a gap between monitors
  or by an application's `ClipCursor` is not recognised as clipped.
  Reading those would mean per-monitor bounds and a Windows-only API in a
  layer that is meant to stay thin; the loop already degrades correctly
  (it cannot reach the target, and says so) rather than reporting a
  success.

The plugin's own fixes — mirrored clicks from swapped markers, `"false"`
parsed as found, a click reported without confirmation, a scroll argument
that never worked — are listed with the plugin below.

### Fixed — a second adversarial review, of the raise-by-clicking work

The same treatment applied again once auto-raise worked: a second model,
the project's invariants, and an instruction to refute rather than agree.
Everything that survived was one category again — **a guard that never
ran, reported as a guard that passed.**

- **The re-read after a raise failed open.** The click's effect is meant
  to be read back rather than assumed, and it was — but a re-read that
  came back as a tool error, or without the window in it, fell through to
  the pre-click window and carried on. That is exactly the assumption the
  re-read exists to replace, and the screenshot after it would have been
  of a rectangle nobody confirmed. It now refuses, saying that whether
  the window came forward is unknown.
- **Where occlusion could not be measured, the plugin filled in "fine".**
  `computer_windows` turned a missing `visible_fraction` into
  `visible_percent: 100`, and a missing `enabled` into
  `accepts_input: true`. On macOS, which measures neither, that meant
  every window was reported as fully visible and accepting input whether
  or not it was — the support table in this file said the guards do not
  run there, while the tool itself said they had passed. Both fields are
  now absent where nothing measured them, the listing says so in words,
  and every answer about such a window carries the fact.
- **Hit-testing for a caption point had no ceiling.** Each probe asks the
  target application to answer on its own UI thread, and
  `SMTO_ABORTIFHUNG` only cuts short a thread Windows already considers
  hung; a merely slow handler spends the whole timeout, up to sixteen
  times per window. Enough of those and the caller times the listing out
  and falls back to a full-screen capture — the one region this feature
  exists to avoid. The enumeration now shares a three-second probing
  budget, and a window past it simply reports no raise point.
- **A drag-area answer was being trusted as "a click here does nothing".**
  Found by the tightened re-read above, on the first real run after it
  landed: raising Chrome moved its title from the page it was on to
  "New Tab" — the raise point was its "new tab" button, which reports
  itself as drag area like the rest of the strip. Before the re-read was
  tightened this would have passed silently, and every screenshot and
  click after it would have been aimed at the wrong page. The scan now
  runs right to left, and the claim that such a click "does nothing else"
  is gone from the docs because it is not true: what makes this safe is
  the re-read, not the hit-test answer.
- **The probing budget was shared but not rationed.** A three-second
  allowance for the whole enumeration still let one unresponsive
  application first in Z-order absorb all of it, leaving every window
  behind it with no raise point — protecting the caller's timeout while
  quietly disabling the feature for everything else. Each window now takes
  at most 0.6s of the three.
- Deliberately **not** changed: a window that is visible enough to work
  with but merely unfocused is still allowed through instead of refused.
  Refusing over that distinction would reject ordinary working
  arrangements, and the occlusion measurement — which is the one that
  decides whether a screenshot is of the right application — has
  already passed by then.

### Added — `adapters/dsh/plugin` (`dsh-clawtouch`, not yet published)

- A DeepSeek Harness plugin that composes the above into
  `computer_click({ target: "the blue Send button" })`: it crops to the
  window, stamps the markers, asks a vision model for the markers and the
  target in one call, solves the rescale, and clicks — returning a
  sentence, never an image. Lives here rather than in a separate repo so
  the plugin and the tools it depends on version together.
- Ships a runtime skill and a synchronous guard that blocks Cmd+Q / Alt+F4
  / Cmd+W: a real HID keystroke lands on whatever window has focus, and on
  a shared machine that is the agent's own session.
- `probe.js --move-test` exercises the device and the coordinate maths
  with **no API key**, which is how the convergence bug above was found.

Fixed in the same review, all of them silent-wrong-click paths:

- **Swapped markers produced a mirrored click, not an error.** If the
  model labelled the bottom-right marker `tl` and vice versa, both axes
  fit with scale −1, the two agreed with each other perfectly, and every
  target came back mirrored through the centre of the image — a fit that
  passes every isotropy check. A negative scale is now refused, and the
  markers are read by name rather than by position in the reply.
- **An anisotropy check that would have rejected working models.** The
  original rule demanded the two axes agree within 25%, but several vision
  models normalise each axis independently onto a fixed square, so a
  1600x900 image legitimately returns scales 44% apart. The check now
  accepts a fit matching *either* a proportional resize or a per-axis
  normalisation, and refuses only what matches neither.
- **`"false"` is not `false`.** Models emit the string as readily as the
  boolean, and `found !== false` turned an explicit "it is not on screen"
  into a click. Coordinate parsing was equally lenient: `Number(null)` is
  0, so a `[null, null]` answer became a confident click on the image's
  top-left corner.
- **A click was reported on the absence of an error rather than the
  presence of a confirmation.** `hid.click` answers with `ok` and
  `clicked`; a reply carrying neither now fails instead of reporting a
  click nobody can vouch for. The same applies to type / key / scroll.
- **An out-of-range click was detected only after it happened.**
  clawtouch-mcp clamps and then genuinely clicks, by design. The plugin
  now checks the addressable screen bounds *before* sending, so a window
  on a monitor the server was not told about is refused instead of
  producing one wrong click and then an error.
- **A point two pixels past the capture edge was passed through.** The
  tolerance exists for sub-pixel rounding, so it is now applied as a
  clamp: an accepted point always lands inside the rectangle that was
  actually looked at.
- **`computer_scroll` never worked.** It passed `amount` where the wire
  argument is `delta`, so every call failed. Found while verifying the
  review's claims against the real tool schemas rather than by the review
  itself.
- **A window behind another window is now refused** rather than described.
  The refusal names the remedy the agent can already carry out: click the
  window's taskbar button, which raises it through the same physical mouse
  — no focus-stealing API involved.

Verified end to end against the real vision model (`qwen-vl-max`), not
just against a stub: five consecutive clicks located from plain-language
descriptions — *"the C button that clears the entry"*, *"the 7 key"*,
*"the plus key"*, *"the 9 key"*, *"the equals button"* — drove a real
calculator to display **16**. On a six-target accuracy pass every target
landed inside the correct button (cells are 79x53 px; the largest error
was 13 px, the median about 7). Calibration is what buys that: the model's
raw answer for the "8" key was 5 px off in both axes before the marker fit
corrected it.

## [0.4.6] — 2026-06-07 — test-only: macOS CI fix for the 0.4.5 Retina guard

### Fixed — test

- `test_explicit_screen_wins_over_detection` over-asserted that
  `_detect_screen` is never called for an explicit `--screen`. The 0.4.5
  Retina guard legitimately consults it (read-only, for a point/pixel
  comparison) on macOS, so the test failed on macOS runners only
  (Windows/Linux skip the darwin-only guard). The test now pins a
  non-darwin platform to isolate the resolution-skip invariant; the
  guard's detection call is covered by `TestRetinaPixelScreenGuard`.
  No runtime change from 0.4.5.

## [0.4.5] — 2026-06-07 — Retina --screen guard · mouse_move magnitude docs · move/batch dead-device hardening

### Added — macOS Retina `--screen` pixel/point guard

- On macOS the OS cursor query returns CoreGraphics **points**, not pixels
  (Retina scales points:pixels 2:1), but `--screen` / clamp / the converge
  loop are pixel-agnostic and trust whatever `WxH` you pass. A physical-pixel
  `--screen` on a Retina display (e.g. `2880x1800` for a `1440x900`-point
  screen) made every absolute click un-convergeable: the point-space cursor
  can never reach the pixel-space target, so the converge loop exhausts
  `MOVE_MAX_ITERS` and returns `ok:false`. The server now warns at startup
  when an explicit `--screen` looks like physical Retina pixels (the
  ~2x-in-both-axes signature, distinguished from a wider multi-monitor
  bounding box that grows in one axis), warning rather than rejecting. The
  `--screen` help text now states it expects logical size (points on Retina).

### Documented — `mouse_move` int16/int8 magnitude contract

- `build_mouse_move` packs signed int16 deltas (±32767), but a USB HID Boot
  Mouse report carries only int8 per axis (-127..127). Adafruit HID's
  `Mouse.move()` splits any `|delta| > 127` into successive reports, so a
  large delta is delivered in full over multiple reports — a stable library
  behavior the firmware relies on and never clamps. This was undocumented;
  `build_mouse_move`'s docstring (with the companion protocol-v1.md note and
  firmware comment) now state the int16 range and the split contract.

### Fixed — move/batch dead-device hang, batch held-state leak, doc + env-hook hardening

- **Death-spiral guard on the move loops.** A dead/unplugged device never
  ACKs a mouse report, and each un-ACKed report blocks the bridge for the
  full per-ACK timeout (~1 s). The glide/converge loops would issue up to
  ~100 slide steps + 10 converge passes → ~110 s of dead-air for a single
  move, and a continue-on-error `hid.batch` multiplied that by op count
  (~19 min for 10 ops), all while stdio is serial-blocked. The loops now
  bail after `MAX_CONSECUTIVE_MOVE_TIMEOUTS` (3) consecutive un-ACKed
  reports (counter resets on any ACK, so a transient single drop still
  rides through), flagging `device_nonresponsive` so callers skip the
  dependent click. `MAX_MOVE_MS` bounded the glide *sleeps*; this bounds the
  *ACK-wait* dead-air it never covered.
- **`hid.batch` stops on device non-response even with `stop_on_error=false`.**
  A dead device can't recover mid-batch, so the run now halts on the first
  op that reports `device_nonresponsive` / an ACK-timeout diagnostic instead
  of grinding every remaining op through its full timeout. Recoverable
  per-op errors (bad arg / firmware ERROR / seq mismatch / no convergence)
  still honor `stop_on_error` as before.
- **`hid.batch` held-state leak in continue-on-error mode.** Cleanup
  (`release_all`) fired only on `stopped_early`, so a `stop_on_error=false`
  run whose `button_up` failed after a `button_down` returned `ok:false`
  yet left the button physically held with no cleanup and no signal. Cleanup
  now also fires when the run had any failure (`failed_index` set); a fully
  clean run still leaves a button held on purpose for a follow-up call.
- **`hid.click` description corrected.** It claimed "Click fires regardless
  of convergence", but the click is (correctly) skipped when the move fails
  (no convergence / cursor unavailable / un-ACKed report). The text now
  matches the gated behavior; a tools/list regression test pins it.
- **`CLAWTOUCH_FAKE_CURSOR` env hook gated to test/mock mode.** The hook is
  now honored only when explicitly enabled (test suite / `--mock` startup);
  on a real-hardware run a stray/leaked value is ignored (warned once) and
  the OS cursor query is used, so a polluted env can't make an absolute
  click compute its delta off a phantom cursor.

312 tests; no behaviour change on real-hardware paths.

## [0.4.3] — 2026-06-05 — screenshot works under hardened-runtime library-validation hosts

### Added — no-Pillow `mss-png` screenshot backend + auto-degrade

`hid.screenshot` previously hard-required both mss **and** Pillow. On a host
Python with a hardened runtime + *library validation* (reported on a bundled
py3.13/arm64 launcher), loading Pillow's native `_imaging` extension is
rejected by macOS:

```
ImportError: dlopen(.../PIL/_imaging...so): code signature ... not valid
for use in process: ... (non-platform) have different Team IDs
```

…and the tool then failed outright with a misleading "install `[screenshot]`"
message — even though the extra was already installed. Platform frameworks
(CoreGraphics) are exempt and mss is pure-Python (ctypes → CoreGraphics), so
mss loads fine; only Pillow's compiled extension is blocked.

- New **`mss-png`** backend: grabs via mss, decimates to logical resolution
  with a pure-Python integer-stride downsample (so it can't re-introduce the
  base64 buffer overflow the Pillow resize prevents), and encodes with mss's
  pure-Python `to_png` (zlib). No native extension → loads where Pillow's
  `_imaging` is blocked.
- **`--screenshot-backend {auto,pillow,mss-png}`** (default `auto`): Pillow
  when its `_imaging` loads, else `mss-png`. The probe is cached, so a failing
  dlopen isn't retried on every screenshot.
- On degrade the call **succeeds** (returns the image) with a metadata
  `note` that translates the dlopen error into a fix instead of erroring:
  run from a Python without library validation, or grant the host the
  `com.apple.security.cs.disable-library-validation` entitlement.
- Metadata gains a **`backend`** field; `scale_x`/`scale_y` stay honest — the
  mss-png path downsamples Retina to logical so scale collapses to ~1.0 like
  the Pillow path (fractional DPI is reported honestly so callers divide
  correctly). The tool description reminds agents to always divide click
  coordinates by `scale_x`/`scale_y`.
- New **`screenshot-min`** extra (mss only): a library-validation-safe
  screenshot install with no native dependency. `[screenshot]` is unchanged
  (mss + Pillow) for JPEG + LANCZOS resize.

### Fixed

- The `pyproject` comment claimed a no-Pillow PNG fallback that did not
  actually exist in the code; the fallback now exists and the comment matches.

### Fixed — pre-release multi-dimension audit

A deep audit before publishing 0.4.3 surfaced one regression and four smaller
conformance gaps, all fixed here:

- **mss-png bypassed the 4M-pixel output cap** (regression, this release): the
  integer decimation factor used `round()`, so a cap-only shrink in the 4–9M
  pixel band (e.g. a 5.94M Retina grab → 4M target = 1.22×) rounded to f=1 and
  returned a full-res multi-MB PNG — the exact base64 overflow the cap exists
  to prevent, on the no-Pillow path that is the default under library
  validation. Now uses `ceil` so the decimated frame always fits the cap.
- Unhandled JSON-RPC **notifications** no longer get a spurious `id:null`
  `-32601` reply (JSON-RPC 2.0 §4.1: never reply to a notification).
- Non-dict `params` now returns **-32602 Invalid params** instead of -32603
  with a leaked Python `AttributeError`.
- The idle-release watcher is **re-armed after a lazy reconnect**, so a single
  tool call right after a reconnect can't hold the COM port forever (HID
  coexistence with the ClawTouch desktop).
- `run_stdio` now emits the `N HID tools + M device tools registered;
  listening on stdio` startup line the READMEs already document.

296 tests (was 274); no behaviour change on the Pillow path.

## [0.4.2] — 2026-06-04 — doc fixes + stdio loop robustness hardening

### Fixed — stdio loop no longer crashes on valid-but-malformed JSON-RPC

An external audit found two ways a non-conforming or adversarial peer could
take the whole stdio session down (a conforming MCP client never hits
either — both are robustness hardening, no behavior change for real hosts):

- **Valid JSON that isn't an object** (`[]`, `"x"`, `5`, `true`) used to
  raise `AttributeError` at `dispatch()`'s `msg.get(...)` — which runs
  *before* the parse-error guard and so slipped through to `run_stdio`'s
  fatal handler, killing the session and dropping every later message.
  `dispatch()` now type-checks and returns JSON-RPC **-32600 Invalid
  Request** (`id: null`), keeping the connection alive — mirroring the
  existing -32700 handling for invalid JSON.
- **Short Content-Length frame**: `_read_exact` returns a truncated buffer
  on EOF; `_read_framed` then `json.loads`-ed it, so a short body whose
  prefix was coincidentally complete JSON could be processed as a whole
  message. It now verifies `len(body) == Content-Length` and raises a parse
  error (-32700) on a short read.

Regression tests added for both paths (unit + end-to-end subprocess).

### Fixed — zh README quick-start tool count (15 → 16)

`README.zh-CN.md` quick-start said "15 个可用工具" while its own inline
breakdown (14 HID + 2 device) sums to 16 — and the server registers 16 by
default (17 with `--allow-screenshot`). Matches the English README and the
`len(tools) == 16` test.

### Fixed — tool description matched pre-0.3.3 convergence constants

The `hid.click` / `hid.move` tool description (which agents read at
`tools/list`) still advertised the convergence numbers from before the
0.3.3 recalibration — "up to 4 iterations, ≤3 px tolerance" and a "3-iter"
glide converge. The shipped values are `MOVE_MAX_ITERS = 10`,
`MOVE_TOLERANCE = 5`, and glide gets the full converge budget. Updated the
description text to match. Description-only — no behavior or API change.

## [0.4.1] — 2026-06-04 — `hid.batch`: pace back-to-back clicks (real-hardware mac dogfood)

### Fixed — consecutive `hid.batch` clicks no longer merged/dropped by the OS

A real-hardware dogfood on macOS (native Minesweeper) found that a
`hid.batch` clicking several vertically-adjacent cells back-to-back had
**only the last click register** — the earlier ones did nothing in the
app, even though every click was sent and ACKed (`clicked: true`). The
HID layer can't observe an app-level drop, so a `delay_ms: 40` on each op
fixed it 100% — a clean A/B (zero gap fails, ~40 ms works).

Root cause: ops ran with **zero inter-op gap**, so two clicks landed too
close in time for the OS/app to treat them as discrete single-clicks
(coalesced or dropped). `delay_ms` defaulting to 0 had made "gets merged"
the default behaviour — the worst failure mode, since the call reports
success while the app didn't act.

Fix (host-side only — wire protocol / firmware unchanged):

- **Click / button ops now get a small default settle gap
  (`DEFAULT_CLICK_SETTLE_MS = 50` ms) when `delay_ms` is omitted**, so
  back-to-back clicks are paced apart. Non-click ops still default to 0.
- An **explicit `delay_ms` (including 0) overrides** the default —
  advanced callers can opt out with `delay_ms: 0`.
- The default gap only fills the space **between** ops (no needless wait
  after the final op); an explicit `delay_ms` is honored even after the
  last op.

## [0.4.0] — 2026-06-04 — `hid.batch`: sequence a short pre-planned action list

### Added — `hid.batch` tool

A new always-on tool that runs a **short, pre-planned sequence of HID
actions (≤10) in one call**, in strict order — collapsing N tool
round-trips into one. Each op is `{type, ...params, delay_ms?}` over the
types `click` / `move` / `button_down` / `button_up` / `key` / `type` /
`scroll`, with the **same semantics as the standalone tools** (absolute
moves run the identical closed-loop converge; `key` accepts the same
`ctrl+c` shorthand). It returns a top-level
`{ok, count, failed_index, stopped_early, released_all, results:[…]}`
where each per-op entry carries the fields its standalone tool would
return (`converged` / `clicked` / `chars` / …).

**Why this lives in OSS.** `hid.batch` is the same kind of host-side
composition over existing HID primitives as `hid.drag` / `hid.hold_key`
and the `move_ms` glide option — it adds **no wire-protocol opcode and no
firmware change**, it just sequences `mouse_move` / `mouse_click` /
`key_combo` / `type_text` / `mouse_scroll` / `mouse_button_*` calls that
already exist. It is a **transport convenience**, not an orchestration
layer: there is no branching, no reading a result mid-sequence, and no
looping, so an `act → observe → decide → act` flow still uses separate
calls. It's useful only for action lists you *already* know (e.g. several
fixed coordinates a solver computed); the logic that produces such a list
is out of scope for this minimal HID layer.

Safety design:

- **Hard cap of 10 ops, enforced in the handler** (not just the schema
  `maxItems`, which a raw JSON-RPC client can bypass). This drives real
  keyboard/mouse on the host and stdio is strictly serial — while a batch
  runs, no other tool call (not even `hid.release_all` to stop it) can get
  in — so a small cap keeps any single batch short-lived.
- **Per-op exception isolation.** A failing op (a too-long `type`, a
  bridge timeout, hardware unavailable) becomes that op's
  `{ok:false, error, bridge_diagnostic}` entry instead of aborting the
  run and discarding the results gathered so far.
- **Held-state cleanup.** With `stop_on_error=true` (default), the run
  halts at the first failure; if a button/key was pressed before the stop,
  `release_all` fires so nothing stays held. A *clean* run is **not**
  auto-released — a batch may intentionally leave a button down for a
  follow-up call (mirrors `hid.mouse_button_down`).
- **Honest `isError`.** The top-level `ok` is the AND of every op, so a
  partial failure (e.g. 3/10) surfaces as `isError:true` with the full
  per-op results intact rather than a false "batch succeeded".

### Changed

- Advertised tool count corrected to **16** (14 HID + 2 device; 17 with
  `--allow-screenshot`) across the README / docs.

## [0.3.3] — 2026-06-04 — absolute-click convergence recalibration (real-hardware mac dogfood)

### Fixed — `hid.click` / `hid.move` no longer refuse near-miss landings (glide mode, macOS)

A real-hardware dogfood on macOS Retina (real Pico, firmware 1.1.2) found
that consecutive `hid.click` calls in **glide mode** (`move_ms > 0`)
intermittently returned `converged: false` / `ok: false` and **skipped the
click** — even though the cursor had landed only 4-7 px from the target,
well inside the clickable element. The click gate added in 0.3.1 ("don't
click where we never confirmed reaching") was correctly refusing; the
underlying convergence was mis-calibrated, not the gate.

A moves-only controlled experiment (faithful re-implementation of the
server's converge/slide, run on real hardware, 12 moves per mode) isolated
the cause — the iteration budget is the *only* variable between the two
absolute paths:

| mode  | converge iters | non-converge |
| ----- | -------------- | ------------ |
| snap  | 4              | 0 / 12       |
| glide | 3              | 2 / 12       |

Glide's post-slide converge was hard-coded to `MOVE_MAX_ITERS - 1` (= 3) on
the assumption that "the slide already landed within tens of px so fewer
settles are enough." Real ballistics refuted that: the slide's final
micro-step is itself amplified, leaving a residual the same order as a
cold-start move (failure trace `62 → 20 → 7 px`, still shrinking ~30 %/pass
— one more pass would have landed it). It was **not** concurrency, rate
limiting, Retina point-vs-pixel scaling, or wrong agent coordinates — the
failing landings were ~4 px from their *own* target (a sequential near-miss
signature, not the hundreds-of-px signature of cursor contention).

Recalibration (host-side only — wire protocol and firmware unchanged):

- **`MOVE_MAX_ITERS` 4 → 10.** A generous *ceiling*, not a budget: the
  converge loop early-exits the instant the residual is within tolerance
  (a normal move still settles in 2-5 passes), so the headroom only costs
  wall-clock on a genuinely struggling move and makes accuracy independent
  of move distance / screen size with no per-distance calibration.
- **Glide post-slide converge now gets the FULL `MOVE_MAX_ITERS`** (was
  `MOVE_MAX_ITERS - 1`). The slide earns no smaller budget.
- **`MOVE_TOLERANCE` 3 → 5 px.** 3 px sat right on macOS's ±2 px cursor-
  report quantization band, so the loop could oscillate at 3-4 px and never
  *terminate* as converged. 5 px is comfortably above the jitter floor and
  still far inside any clickable target (smallest common UI ~16 px). The
  observed 4-7 px near-misses now converge and click via the normal path —
  i.e. "close enough to click" is expressed as what counts as on-target,
  not a fail-open path that would weaken the 0.3.1 no-false-success contract.
- **Non-convergence `hint` reworded** — no longer leads with "competing
  input device" (which mis-attributed a calibration issue to an external
  cause); it now notes the actual landing is usually within a few px and
  surfaces the iteration count.

Constants stay baked in (no CLI knobs). `tests/test_move_convergence.py`
updated: the glide-budget test now pins the full `MOVE_MAX_ITERS` (was
`- 1`), plus a new high-amplification regression that asserts a glide move
needing more than the old 3-pass budget now converges instead of tripping
the click gate. The two `examples/computer_use` reference reimplementations
track the new defaults. 241 → 242 tests; zero regression.

## [0.3.2] — 2026-06-02 — registry packaging (mcp-name marker + --mock Dockerfile)

### Added — official MCP Registry readiness

- README now carries an `mcp-name: io.github.tinqiao-oss/clawtouch-mcp` marker, so the
  package can be claimed on the official MCP Registry (registry.modelcontextprotocol.io),
  whose ownership check reads this marker from the published package README.
- A `--mock` `Dockerfile` for registry / CI introspection (e.g. Glama): the container has
  no USB hardware, so it starts in mock mode and still answers introspection requests
  (lists the full tool surface).

Metadata / packaging only — no code or tool behaviour changes; test suite unchanged.

## [0.3.1] — 2026-06-02 — stdio UTF-8 frames · composed-tool failure propagation

### Fixed — stdio frames are UTF-8 on every host locale (Chinese Windows / cp936)

The line-delimited (newline) stdio branch — the MCP-stdio default — wrote
JSON through the locale-encoded `TextIOWrapper` (`writer.write(...)`). On a
non-UTF-8 console code page (cp936 / GBK on Chinese Windows, where a piped
`sys.stdout.encoding` defaults to `'gbk'`) any non-ASCII byte got
GBK-encoded. A single em-dash in a tool description was enough: `tools/list`
came back as GBK and a UTF-8 MCP client raised
`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa1` — the session
never established. The framed (Content-Length) branch was already correct
(it wrote `data.encode("utf-8")` via `writer.buffer`); only the newline
branch was affected. Both branches now write UTF-8 **bytes** via
`writer.buffer`, so the wire encoding is UTF-8 regardless of host locale.
Workaround for older builds: launch with `PYTHONUTF8=1` (or
`PYTHONIOENCODING=utf-8`). New regression suite
`tests/test_stdio_utf8_encoding.py` (4 tests). 237 → 241 tests.

### Fixed — composed tools now propagate HID sub-call failures (no false success)

The composed tools (`hid.click`, `hid.hover`, `hid.drag`, `hid.hold_key`)
and the stepped-move helpers issue several bridge sub-calls in sequence.
Several of them only checked for an `error` and ignored an `ok: False`
ACK from an underlying move / press / release — so a move the firmware
never acknowledged (timeout / seq mismatch / firmware ERROR / parse error)
could be followed by a click, drag, or keypress anyway, and the tool
returned success. For an agent driving real hardware that is the worst
failure mode: it builds on a click that never landed.

Now every sub-call's ACK is honoured:

- **`hid.click`** does not click when the positioning move fails
  (cursor unavailable / no convergence / a relative-move report not ACKed),
  and surfaces the move failure unchanged; a failed click ACK is reflected
  in `ok` (new `clicked` field) and never masked.
- **`hid.hover`** reports the move failure instead of claiming `ok: True`
  for a hover that never reached the target.
- **`hid.drag`** aborts *before pressing* if the move to the source point
  fails (no press from an unconfirmed position), and AND-s the
  press / drag-move / release ACKs into the final `ok` (new `down_acked` /
  `up_acked` diagnostics). The release still runs in `finally`, but a
  successful release no longer upgrades a failed drag back to success.
- **`hid.hold_key`** AND-s the press and release ACKs (new `press_acked` /
  `release_acked`); the release still runs in `finally`.
- **`_stepped_relative_move`** now returns `ok` = AND of every emitted
  report's ACK; **`hid.move` (relative + `move_ms`)** keeps that real `ok`
  instead of the previous unconditional `True`.
- Absolute moves keep using closed-loop convergence (OS cursor = ground
  truth) for `ok`; a dropped ACK whose cursor still reached the target is
  recorded as a `move_acked` / `slide_acked: False` diagnostic without
  blocking the verified-on-target action.

New regression suite `tests/test_composed_tool_failure_propagation.py`
(20 tests) covers each sub-call failure for click / hover / drag /
hold_key / stepped move, the mid-gesture cleanup paths, and the happy
paths. 217 → 237 tests.

### Added — self-interrupt heads-up (cmd+q / alt+f4)

Real USB HID has no app-level addressing — keystrokes go to whatever
window is frontmost. When the server shares a machine with the agent
driving it and the agent app is frontmost, `hid.key("cmd+q")` /
`hid.key("alt+f4")` quit the agent itself mid-task. The server now logs a
**one-time, warn-only** stderr heads-up the first time it sends such a
quit-class combo — it never blocks or swallows the keystroke (the same
combo is legitimate against a remote target). A new "Known footgun:
self-interrupt" section in `INTEGRATIONS.md` (plus pointers in the macOS /
Windows setup guides) documents the full key table and mitigations
(click the target first / drive a remote target / self-regulate on focus).

### Documentation — type non-ASCII / Chinese via clipboard paste

The macOS and Windows setup guides now lead with the IME-bypass pattern
(put text on the clipboard, then `hid.key("cmd+v")` / `"ctrl+v"`) as the
robust way to enter Chinese, emoji, and punctuation — `hid.type` sends raw
US-layout keycodes and cannot produce non-ASCII through an active IME.
Input-source switching (ABC) stays as the lighter ASCII-only option.

### Fixed — Computer Use example + async test deps (codex cross-check)

- **Anthropic Computer Use demo** now uses tool type `computer_20251124` +
  beta `computer-use-2025-11-24` — the pairing that actually supports the
  default `claude-opus-4-8` model. The old `computer_20250124` /
  `computer-use-2025-01-24` is for Sonnet 4.5 / Haiku 4.5 / Opus 4.1 and
  would return an API error on Opus 4.x. README references updated too.
- **Async tests no longer silently skip.** Added a `test` extra (`pytest` +
  `pytest-asyncio`), set `asyncio_mode = "auto"`, and made
  `tests/conftest.py` fail loudly when `pytest-asyncio` is missing — the 15
  `@pytest.mark.asyncio` tests used to skip (false-green) on a bare
  `pip install pytest`. CONTRIBUTING + CI now use `.[screenshot,test]`.
- **INTEGRATIONS.md** troubleshooting corrected: a missing Pico mounts an
  `UnavailableBridge` (clear error + lazy retry), it does NOT silently fall
  back to mock; the real log line and the `--mock` opt-in are documented.
- README now documents that `hid.type` is ASCII / US-layout text and strips
  control characters by default (use `hid.key("enter")` / `hid.key("tab")`).
- Added a `Documentation` project URL.

### Added — tool-selection guidance for LLM clients

Two complementary mechanisms ensure LLMs reliably pick `hid.*` tools
when appropriate, instead of defaulting to file APIs or refusing the
task:

1. **Server-level `instructions`** in the MCP 2024-11-05 `initialize`
   response. Tells the client "prefer `hid.*` when no API or
   automation path exists for the target application, or when the
   user explicitly requests physical keyboard / mouse input."
   Recognised by Claude Desktop, Cursor, Hermes, ChatGPT Desktop and
   other spec-compliant clients.
2. **Per-tool `HID_PREFIX`** prepended to every `hid.*` tool's
   `description`. Tool-selection-time guidance — visible even if the
   client ignores the server-level `instructions` field. The 13
   baseline `hid.*` tools + the opt-in `hid.screenshot` all carry the
   prefix; `device.*` tools are unaffected (read-only diagnostics, no
   selection ambiguity).

This addresses a real LLM-behavior risk: the original `hid.*`
descriptions were physics-detailed (closed-loop convergence, OS
pointer ballistics) but had no application-layer anchor, so an LLM
seeing *"open WPS Office"* had nothing in the description telling it
*"this is the right tool for that."* The guidance explicitly frames
`hid.*` as a fallback layer that activates when other paths fail or
when the user names ClawTouch / physical input directly.

### Fixed — Computer Use examples

- `examples/computer_use/claude_demo.py` and `openai_cua_demo.py` no
  longer report *"drag not supported by current firmware"*. They now
  compose a real drag from the v1.1 button-hold primitives
  (`bridge.mouse_button_down` → glided `mouse_move` → `bridge.mouse_button_up`,
  with `try/finally` so the button is always released) and handle the
  `left_mouse_down` / `left_mouse_up` actions. The `hid.drag` tool and
  the root README's tool table already advertised v1.1 drag; only these
  two reference scripts were stale.

## 1.1.1 (protocol layer — package stays 0.3.0) - 2026-05-29

<!-- Not a package release / git tag — intentionally unbracketed so it is
     not a dangling compare-link. See the Note below. -->

### Changed (BREAKING vs <= 1.1.0)

- Unified keyboard payload byte order to `[modifiers, keycode]`. KEY_PRESS (0x20) and KEY_RELEASE (0x21) previously used `[keycode, modifiers]`; they now match KEY_COMBO (0x23) and the USB HID keyboard report layout (modifier byte first). Breaking wire change for KEY_PRESS/KEY_RELEASE vs firmware <= 1.1.0 — flash firmware 1.1.1 in lockstep. Pre-publish correction; the protocol has not been publicly released.

> **Note:** this is a *protocol-layer* version (`clawtouch-hid-protocol` 1.1.1).
> The `clawtouch-mcp` package version is unchanged at 0.3.0 — no MCP tool
> surface or argument changed; only the wire byte order of the keyboard
> frames built by `clawtouch_mcp.protocol` was unified.

### Fixed — docs & examples (pre-publish sweep)

- Replaced the stale `0.2.3` literal in the README / zh-CN README startup
  transcripts and `docs/windows-setup.md` `device.info` sample with the
  real package version `0.3.0` (the `v0.2.3+` "since version" markers are
  intentionally left).
- Corrected the advertised tool count to **15** (13 HID + 2 device) in the
  zh-CN README, and expanded the abbreviated `tools/list` example in both
  READMEs to list all 13 HID tools (was 9, omitting the v1.1 drag/key tools).
- zh-CN README now links the **Windows** setup guide (was macOS-only) and
  carries the `Commercial: clawtouch.cn` badge for EN/zh parity.
- Clarified that `--ops-per-sec` rate-limits *tool calls*, not individual
  HID reports (one `hid.drag` / long `hid.type` emits many).
- `examples/computer_use`: removed a dead `MouseButton, modifiers_to_mask`
  import; made the screenshot demos robust to `mss.MSS` vs `mss.mss`
  (and floored `mss>=10.2` in the `[screenshot]` extra, where uppercase
  `MSS` first appears); added a `--model` flag (default tracks the current
  GA Opus) instead of a hard-pinned model; recommended `pip install -U`
  for the beta/preview SDKs; fixed the README to say
  `client.beta.messages.stream`.

### Changed — server hardening

- `hid.hover` now lower-clamps `duration_ms` (`max(0, …)`) to match
  `hid.hold_key`.
- `hid.type` reports the number of characters **actually sent** (control
  bytes are stripped by default, so a lone `"\n"` now reports `chars: 0`
  rather than `1`).
- `MockBridge` / `UnavailableBridge` `type_text` gained the `allow_control`
  keyword for signature parity with `SerialHidBridge`.
- `build_key_press` / `build_key_release` / `build_key_combo` docstrings
  warn that press/release take positional `(keycode, modifiers)` while
  combo takes `(modifiers, keycode)` — prefer keyword args.

### Added — tests & CI

- Cross-repo byte-equality suite now covers the v1.1 drag opcodes
  (`MOUSE_BUTTON_DOWN/UP`) and a drag round-trip.
- New `tests/test_bridge_key_byte_order.py` exercises the real
  `SerialHidBridge.key_press` / `key_release` serialization path end-to-end
  and locks the wire payload to `[modifiers, keycode]` (the server tests
  use `MockBridge`, which never builds a frame).
- The cross-repo CI job now fails (not warns) when the sister
  `clawtouch-hid` repo is unreachable, so the byte-equality net can no
  longer be silently skipped on a green run.

## [0.3.0] — 2026-05-28 — Drag + hold gestures (protocol v1.1, Anthropic CUA tool-set parity)

### Added — six new MCP tools matching Anthropic Computer Use action set

`clawtouch-mcp` now exposes the v1.1 wire opcodes plus three composed
gestures, bringing the HID tool surface to 15 (was 9; `hid.screenshot`
remains opt-in via `--allow-screenshot`):

- `hid.mouse_button_down(button)` — press without releasing. Matches
  CUA `left_mouse_down`. Wraps the v1.1 `MOUSE_BUTTON_DOWN` (0x13) frame.
- `hid.mouse_button_up(button)` — release. Matches CUA `left_mouse_up`.
  Wraps v1.1 `MOUSE_BUTTON_UP` (0x14). Idempotent on the firmware side.
- `hid.drag(from_x, from_y, to_x, to_y, button="left", move_ms=300, relative=False)` —
  composed: snap-move to source → `mouse_button_down` → glided absolute
  move to destination → `mouse_button_up`. Matches CUA `left_click_drag`.
  Release is wrapped in `try/finally` so a mid-drag exception still
  releases the button (a stuck mouse button corrupts subsequent host
  input far worse than a partial drag).
- `hid.key_press(key, modifiers)` — press a key (or shortcut) without
  releasing. Useful for "hold shift while clicking N times" multi-select
  patterns where atomic `hid.key('shift+click')` doesn't help.
- `hid.key_release(key, modifiers)` — release. Pass no arguments to
  release ALL held keys + mouse buttons (panic stop, same as
  `hid.release_all`).
- `hid.hold_key(key, duration_ms, modifiers)` — press → sleep →
  release. Matches CUA `hold_key`. Release runs in `try/finally` so
  the key cannot get stuck on exception.

**Bridge surface** (`SerialHidBridge`): four new async methods —
`mouse_button_down(button)`, `mouse_button_up(button)`, `key_press(key,
modifiers)`, `key_release(key, modifiers)`. `MockBridge` and
`UnavailableBridge` updated in lockstep so `--mock` and
unavailable-hardware paths stay covered.

**Protocol module**: two new builders (`build_mouse_button_down` /
`build_mouse_button_up`) + two new `CommandType` members in
`clawtouch_mcp.protocol`. `PROTOCOL_VERSION` bumped to `1.1.0`.

### Tests

Six new test cases in `TestV11DragAndHold` (`test_server.py`) verify:
- direct down/up calls hit the MockBridge with the right button name
- `hid.drag` emits the press-move-release sequence in order (press
  before destination move, release after)
- mid-drag exception in the glided move still triggers `button_up`
  (the `try/finally` safety net)
- `hid.key_press` / `hid.key_release` round-trip correctly
- `hid.key_release` with no args translates to release-all
- `hid.hold_key` emits press → release in order
- existing tool-count guards updated: 15 baseline + 1 screenshot
  (was 9 + 1)

### Changed

- `README.md` + `README.zh-CN.md`: Tools exposed table gains a Since
  column with v1.0 / v1.1 markers; tool-count phrases updated
  (9 → 15)
- `clawtouch_mcp.protocol.PROTOCOL_VERSION`: `1.0.0` → `1.1.0`

### Compatibility

- Requires `clawtouch-hid-protocol >= 1.1.0` and firmware `>= 1.1.0`.
- Older firmware will respond with `ERR_UNKNOWN_COMMAND` (0x01) on
  `hid.mouse_button_down` / `hid.mouse_button_up` / `hid.drag`. Hosts
  can fall back to `hid.click` for non-drag scenarios.
- v1.0 tools (`hid.click` / `hid.move` / `hid.type` / `hid.key` / etc.)
  are byte-for-byte unchanged.

## [0.2.9] — 2026-05-27 — Closed-loop convergence for absolute moves (macOS pointer-ballistics fix)

### Fixed — `hid.click` / `hid.move` / `hid.hover` snap mode lands accurately on macOS

Field-reported by a macOS dogfood run on Ventura ARM64: a single
fire-and-forget `bridge.mouse_move(dx, dy, relative=True)` overshoots
or undershoots by 10–90 px because macOS non-linearly scales single
HID deltas (~110% amplification in the low-speed segment of the
pointer-ballistics curve). The server returned `ok=true` while the
cursor was still drifting, so any follow-up `hid.click` could land
on the wrong UI element.

Measured residuals on a 2-pass control experiment (Target 1 = short
distance, Target 2 = long reverse):

| Target          | Pass 1 residual | Pass 2 residual | Pass 3 residual |
| --------------- | --------------- | --------------- | --------------- |
| `(300, 200)`    | 55 px           | 15 px           | —               |
| `(1200, 800)`   | 71 px           | 26 px           | 7 px            |

Per-pass residual shrinks to ~30% of the previous pass, but the
amplification also applies to short residual corrections, so a
fixed N-pass loop overshoots back the other way. The fix is a
closed-loop converge with a tolerance check on every iteration:

```
target_x, target_y = clamp(target)
for i in range(MOVE_MAX_ITERS):           # 4
    cur = OS cursor query
    dx, dy = target - cur
    if |dx| <= MOVE_TOLERANCE and |dy| <= MOVE_TOLERANCE:  # 3 px
        return converged
    bridge.mouse_move(dx, dy, relative=True)
    sleep(MOVE_SETTLE_MS)                  # 20 ms ≈ 2× HID cycle
return not-converged (with actual position + residual)
```

Constants are baked in (no CLI knobs); values are calibrated against
the measured residual curve so 4 iterations land within ≤3 px on
every test target. On Windows / X11 the OS doesn't ballistics-scale
single deltas, so pass 1 already lands on target and the loop
short-circuits on iteration 2 with no extra cost.

#### Snap mode (`move_ms=0`, default)

`_move_to_absolute` runs the converge loop with `max_iters=4`.

#### Glide mode (`move_ms>0`)

`_stepped_move_to_absolute` keeps the linear-interpolation slide
unchanged (so demos still look smooth), then runs the same converge
loop with `max_iters=3` after the slide finishes — the slide already
landed within tens of pixels so 3 settles is sufficient.

#### Return-value schema (breaking on snap + glide paths)

`hid.click` / `hid.move` / `hid.hover` returns now include:

- `x`, `y` — **actual** landing coordinates (may differ slightly
  from target on platforms with non-linear pointer ballistics)
- `target_x`, `target_y` — original requested target (echoes the
  request)
- `converged: bool` — `true` when residual ≤ MOVE_TOLERANCE
- `iters: int` — number of converge iterations actually run
  (`0` = already on target, `1` = perfect first attempt, …)
- `residual_x`, `residual_y`, `hint` — present only when
  `converged: false` so the agent can diagnose what happened

`ok` now reflects convergence (was: "bridge call succeeded", which
in practice was always `true`). The `hid.click` path overlays the
`mouse_click` success on top, so `hid.click.ok` still means "click
was emitted." `hid.move.ok` and `hid.hover.ok` now mean "cursor
reached the target."

Removed `dx` / `dy` from the absolute-mode return value — there is
no single delta any more (multi-iteration). The `relative=true`
fast path still returns `dx` / `dy` because it stays single-shot.

#### Tests + mock infrastructure

`MockBridge.mouse_move` now lazily seeds and updates a process-
local cursor state (`cursor._FAKE_DYNAMIC_STATE`) so the converge
loop terminates in mock — without this, mock fire-and-forget would
never visibly land. Existing tests that pinned `dx` / `dy` were
updated to assert the new `target_x` / `target_y` / `converged`
fields; tests that monkey-patched `get_cursor_position` directly
now use `cursor._seed_fake_cursor(x, y)` instead.

Added `tests/test_move_convergence.py` (6 tests) covering:

- already-at-target / within-tolerance short-circuits with `iters=0`,
- simulated 110% amplification converges within `MOVE_MAX_ITERS`,
- stuck cursor (mock that drops the delta) bails after
  `MOVE_MAX_ITERS` with `converged=false` / `ok=false` / `residual_*`
  populated,
- glide mode post-slide converge under simulated amplification,
- glide mode converge stage gets `MOVE_MAX_ITERS - 1` budget (3).

181 → 187 tests; zero regression.

### Added — Related Work section in README (EN + zh-CN)

New `## Related work` / `## 相关工作` section between FAQ and the
open-source roadmap. Splits the MCP / Computer-Use ecosystem into
software-only MCP servers running on the target PC (PyAutoGUI-style:
[`domdomegg/computer-use-mcp`](https://github.com/domdomegg/computer-use-mcp),
[`AB498/computer-control-mcp`](https://github.com/AB498/computer-control-mcp),
[`mcp-pyautogui`](https://github.com/hathibelagal-dev/mcp-pyautogui),
ByteDance [UI-TARS](https://github.com/bytedance/UI-TARS-desktop)) vs
hardware-bridge MCP servers
([`sunasaji/mcp-serial-hid-kvm`](https://github.com/sunasaji/mcp-serial-hid-kvm))
— and cites CMU's [HIDAgent](https://arxiv.org/abs/2602.00492) as the
closest academic peer in hardware budget. Avoids any "first / only"
claims.

### Fixed — comment accuracy (external audit, codex)

- `bridge.py:317` — ERROR opcode comment said `cmd_type=0x41`; actual
  protocol constant is `CommandType.ERROR = 0xFF` (see
  `clawtouch_mcp/protocol.py:36`). Fixed the inline comment.
- `bridge.py:477` — `release_all` docstring said "send KEY_RELEASE with
  no payload"; the call actually sends `KEY_RELEASE` with
  `keycode=0 / modifiers=0` (2-byte payload) as the wire-level
  panic-stop signal. Docstring now states this accurately.

No behavior change — comment/docstring only.

## [0.2.8] — 2026-05-27 — Optional `move_ms` path stepping for visible cursor motion

### Added — `move_ms` argument on `hid.click` / `hid.move` / `hid.hover`

The current behavior — a single HID mouse report containing the full
(dx, dy) — makes the OS cursor teleport to the target in one frame.
That's the right baseline for raw HID transport (no behavior
modification, every command 1:1 with the wire) but it's hard to track
visually when recording a demo: viewers can't tell *what* the agent
just did because there's no motion to follow.

Optional argument **`move_ms`** (default `0`, max `MAX_MOVE_MS=5000`)
breaks the move into ~10 ms HID reports over the requested total time:

```jsonc
// Click at (500, 400) over 200 ms (20 stepped HID reports)
{ "tool": "hid.click", "arguments": { "x": 500, "y": 400, "move_ms": 200 } }
```

Step count is `clamp(move_ms // 10, 4, 100)` — minimum 4 so even a
very short ``move_ms`` produces visible motion, maximum 100 so a
typo / runaway agent can't lock the handler. Linear interpolation
only: no curves, no tremor, no dwell variance. Same UX convenience
PyAutoGUI offers as ``duration=``.

`move_ms = 0` (default, the omitted case) goes through the original
single-shot path unchanged — **strict backward compatibility** with
every pre-v0.2.8 caller.

### `hid.hover` semantics clarified

`hid.hover` already had a `duration_ms` argument meaning "idle time
AFTER reaching the target". Adding `move_ms` here would have been
ambiguous (path duration vs idle duration), so the two arguments
stay separate:

- `move_ms` — time spent on the move ITSELF (path stepping; default 0)
- `duration_ms` — idle time AFTER reaching the target (default 500)

Tool description clarified in the schema.

### Notes

- Both **absolute** mode (server queries OS cursor position) and
  **`relative=true`** mode (caller supplies pixel delta directly)
  support `move_ms`. In relative mode the agent-supplied delta is
  chunked; in absolute mode the OS-cursor-derived delta is chunked.
- 9 new regression tests pin: default unchanged / N reports emitted /
  per-step deltas sum to total move / hover decouples both args /
  step count clamped at 100 / zero-distance no-op. Total 172 → 181.

### Why this lives in OSS and not just the demo layer

`hid.click` / `hid.move` / `hid.hover` are the surface every MCP
client sees; making the visual smoothness opt-in at the tool layer
means *every* downstream agent / IDE / framework gets it
consistently when they pass `move_ms`, without each integration
re-implementing path interpolation around the same MCP server.
The closed-source main app does its own richer cursor work on top
of the same hardware — `move_ms` here is the bare-minimum
animation primitive, not a replacement for that layer.

## [0.2.7] — 2026-05-27 — API consistency + docs corrections (mac dogfood round 2)

The same macOS Retina dogfood session that produced 0.2.5 (screenshot)
and 0.2.6 (build backend) surfaced three more papercuts: an API
inconsistency, a wrong Claude Code config path in the integrations
doc, and an incomplete `.gitignore` in the sibling skills repo.

### Changed — `bridge.device_info()` is now `async`

All three bridge classes (`SerialHidBridge`, `MockBridge`,
`UnavailableBridge`) had `device_info()` as a sync method while
every other public method on the same class (`connect`, `close`,
`ping`, `mouse_move`, `type_text`, `release_all`, …) is `async`. New
users learn the API from those and then try
``await bridge.device_info()`` first, which used to fail with
``TypeError: object dict can't be used in 'await' expression``.

device_info is now `async` on all three classes; the in-tree caller
``ClawTouchMcpServer._tool_device_info`` was updated to ``await``.
A regression test (`test_device_info_is_async_across_all_bridges`)
uses ``inspect.iscoroutinefunction`` to pin the contract so the
inconsistency can't sneak back. 171 → 172 tests.

This is technically a breaking change: external callers that did
``info = bridge.device_info()`` (no await) now get an unawaited
coroutine. The fix at the caller is to add ``await`` — and the
unawaited-coroutine warning Python emits is loud and points right
at the call site.

### Fixed — `examples/integrations/INTEGRATIONS.md` Claude Code path

The doc said `~/.claude/mcp.json` for the Claude Code CLI's global
MCP config. That path doesn't exist. The actual location is
`~/.claude.json` — a *file* in the home directory, not a `mcp.json`
inside a `.claude/` *folder* — with `mcpServers` as the top-level
key. Users following the wrong path saw their config silently
ignored.

The section now offers three setup paths:

1. `claude mcp add clawtouch -- clawtouch-mcp --screen 1920x1080`
   (the one-liner; works if `claude` is on PATH)
2. Hand-edit `~/.claude.json` (correct path)
3. Project-scoped `.mcp.json` at repo root (unchanged)

…plus a note that Claude Code CLI doesn't hot-reload MCP config
either, you have to exit the session (`Ctrl+D` / `/exit`) and start a
new one for changes to take effect.

### Fixed — `clawtouch-skills/.gitignore` was missing Python entries

The skills repo is markdown-only today but its `.gitignore` only
covered OS / IDE noise — no `__pycache__/`, no `*.egg-info/`, no
`.pytest_cache/`, no `build/`, no `dist/`, no `.venv/`. If anyone
ever drops a helper script (link checker, schema validator, lint),
artefacts will leak. Brought the file up to parity with the
clawtouch-mcp and clawtouch-hid `.gitignore`s as a preventive
measure.

## [0.2.6] — 2026-05-27 — Build backend switched to hatchling

### Fixed — install-from-source kept failing on macOS after `pip install -e .`

Same macOS Retina test session that produced the [0.2.5](#025--2026-05-27--retina-screenshot-fix-real-world-macos-report)
screenshot fix hit a second, unrelated footgun: after the user ran
``pip install -e .`` (editable) followed by a non-editable
``pip install /path/to/clawtouch-mcp[screenshot]``, install crashed with

```
error: [Errno 2] No such file or directory:
'build/bdist.macosx-11.0-arm64/wheel/./clawtouch_mcp-0.2.4-py3.12.egg-info'
```

The setuptools backend stages the wheel under
``build/bdist.<platform>/wheel/<pkg>-<version>-py<X.Y>.egg-info``. The
version is **embedded in the path** — when a later install runs at a
different version (after the user pulls a new tag, or just after a
local version bump) setuptools tries to clean the old staging dir at
the new path and trips a FileNotFoundError. Worse, the failed install
leaves another stale ``build/`` behind so the next attempt fails the
same way; the only escape is ``rm -rf build/ *.egg-info/``. That isn't
documented anywhere and we won't be hand-holding every external
developer through it once the repo goes public.

### Changed

- **Build backend swapped from `setuptools.build_meta` to `hatchling.build`.**
  Hatchling has no egg-info legacy, builds wheels in an isolated temp
  directory (so the source tree stays untouched after `python -m
  build`), and its editable install path drops a small `.pth` in
  site-packages rather than an egg-info on disk. No more stale
  artefacts to invalidate the next install.
- **sdist contract is now explicit in pyproject.toml.** The new
  ``[tool.hatch.build.targets.sdist].include`` array lists every file
  type a release tarball ships — `clawtouch_mcp/`, `tests/`, `docs/`,
  `examples/`, top-level `README*.md`, `CHANGELOG.md`, etc. Anything
  not in the list (build artefacts, .pytest_cache, __pycache__, IDE
  settings, virtualenvs) cannot leak into the tarball even when a
  developer's working tree is dirty.
- **No version-embedded build paths.** The class of FileNotFoundError
  that triggered this fix is structurally impossible with hatchling.

### Verified

- 171 unit tests pass unchanged (same code, just a different build
  invocation under PEP 517).
- `python -m build` post-build state: only `dist/` is created; source
  tree is otherwise untouched (no `build/`, no `*.egg-info/`).
- sdist tarball inspection: 27 files, all from the explicit include
  list. No stale artefacts.
- Reproduction: planted fake `clawtouch_mcp.egg-info/PKG-INFO`
  declaring `Version: 0.2.4` and a stale
  `build/bdist.win-amd64/wheel/clawtouch_mcp-0.2.4-py3.12.egg-info/`
  directory, then ran `pip install .` in a fresh venv. With setuptools
  this would FileNotFoundError; with hatchling the install succeeds
  and reports `clawtouch_mcp.__version__ == '0.2.6'`.

### Migration note

External developers who previously ran `pip install -e .` against the
old setuptools build can keep their `build/` and
`clawtouch_mcp.egg-info/` directories — they're now ignored by
hatchling. No action required; `rm -rf build/ *.egg-info/` only
matters if they want a tidy working tree.

## [0.2.5] — 2026-05-27 — Retina screenshot fix (real-world macOS report)

### Fixed — `hid.screenshot` overflow on high-DPI displays

A user testing the MCP server on Apple Silicon (logical 1512x982 /
physical 3024x1964, 2x scale) reported that every `hid.screenshot`
call truncated the result and the agent never saw the image. Root
cause was a units mismatch hiding a 4M-pixel cap:

```python
pixels = monitor["width"] * monitor["height"]   # mss returns LOGICAL on macOS
if pixels > MAX_SCREENSHOT_PIXELS:               # 1.48M < 4M, passes
    raise ValueError(...)
shot = sct.grab(monitor)                         # but grab returns PHYSICAL
png = mss.tools.to_png(shot.rgb, shot.size)      # PNG is 3024x1964 = 5.94M px
```

A ~3 MB base64 PNG then went into `{"content": [{"type": "text", ...}]}`
— the tool-result text envelope — and Claude Desktop / Claude Code
truncated it to a side file the agent couldn't read.

**Fix is architectural, not a wider cap:**

- **MCP image content type.** Screenshot tool returns an `ImageResult`
  marker which `_on_tool_call` translates into the spec-standard
  `{"type": "image", "data": ..., "mimeType": ...}` content entry.
  Clients route image content through their vision-token path, not
  the tool-result text buffer.
- **DPI-aware auto-resize.** Full-screen captures auto-downsample
  from physical pixels back to the configured logical screen size
  when the physical buffer is ≥1.2x bigger. On macOS Retina this
  collapses 3024x1964 → 1512x982; on Windows >100% DPI it collapses
  similarly; on Linux / 100% DPI it's a no-op. Pillow LANCZOS resize.
- **JPEG default.** New `format` param (`"jpeg"` / `"png"`, default
  `"jpeg"` at quality 80). Random-noise 1512x982 JPEG q80 ≤ 1 MB
  worst case; typical desktop content is ~150 KB.
- **Output-pixel cap.** `MAX_OUTPUT_PIXELS = 4_000_000` now measured
  on the *resized* image (not the raw mss grab), so it's a real
  defence against giant region requests instead of a no-op on Retina.
  Oversized requests are silently ratio-downsampled — agents see
  `width / height / raw_size` in metadata so they can tell.
- **Pillow added to `[screenshot]` extras.** `mss` still drives the
  capture; Pillow handles resize + JPEG encoding.

Behaviour change for callers: the result no longer has a `base64`
field at the top level. Image data flows through MCP image content;
metadata (`width / height / scale_x / scale_y / format / raw_size`)
flows through the sibling text content. Agents that read `scale_x` /
`scale_y` and divide screenshot coords keep working — the values
collapse to ~1.0 after the resize, so the division becomes a no-op.

`tests/test_screenshot_overflow.py` reproduces the Retina mismatch
with a mocked `mss` and pins all of the above (9 new tests).

## [0.2.4] — 2026-05-26 — Cumulative audit fixes (rounds 4–6)

This release rolls up audit work that landed since 0.2.3 — internal
4-agent round 4, multi-perspective round 5, and codex external
round 6. Each section below is preserved verbatim from the original
audit commits; the `## [Unreleased]` header was closed here.

### Fixed — internal deep audit (round 4)

A clean-up audit (four parallel agents, no specific external prompt)
on top of codex rounds 1-3 surfaced ~17 additional code-level
issues across server / bridge / CLI / examples. All P0 + P1 fixed in
this commit; P2/P3 stay in the backlog.

**P0 — MCP spec compliance**

- **`tools/call` exec errors now return `result.content + isError:true`,
  not JSON-RPC `-32000`.** Per MCP 2024-11-05 spec, JSON-RPC errors
  are reserved for protocol-layer faults; tool execution failures
  (rate limit, bridge timeout, hardware unavailable, validation
  errors, unknown tool name) must surface as `isError` content so
  the agent can read the message and react. Previously every
  `ValueError` / `RuntimeError` from a handler bubbled to
  `dispatch`'s `except Exception` and became a generic JSON-RPC
  error invisible to compliant clients (Claude Desktop, Cline).
  `_on_tool_call` now catches handler exceptions itself and includes
  the bridge's `last_error_detail` (timeout reason, seq mismatch,
  firmware ERROR code) inline. `unknown tool` likewise returns
  `isError` content listing the available tools.
- **Malformed JSON in stdio no longer crashes the server.** A single
  bad line (junk on stdout from a launcher script, BOM, blank `{`)
  used to raise `JSONDecodeError` at `json.loads(first)` /
  `json.loads(text)` in `run_stdio`, blow past the `except Exception`,
  and kill the session. Per JSON-RPC 2.0 spec, parse errors must
  return `{error: {code: -32700}}` and the connection should stay
  open. Now per-message `try/except json.JSONDecodeError` writes a
  -32700 response and continues.
- **Bridge ACK timeout no longer leaks stale bytes onto the next
  request.** `_send_raw` used to write straight to the serial line
  without flushing pyserial's input buffer; any residual bytes from
  a prior aborted request (a `0xAA` byte in payload coordinates,
  for example) could re-sync the parser onto mid-frame data and
  either fail checksum repeatedly or — worse — accept a stale ACK
  as the response for the new request (silently firing the wrong
  HID action). Now: `reset_input_buffer()` before every write, AND
  every response's `seq_id` is verified against the request's;
  mismatch is rejected as a stale ACK.
- **Windows DPI awareness now enabled unconditionally on server
  start.** Previously `SetProcessDpiAwareness(2)` only ran inside
  `_detect_screen` — when the user passed `--screen WxH`
  explicitly, the hook never fired, and on a 125%-scaled Windows
  host `GetCursorPos` returned logical (scaled) pixels while the
  `--screen` clamp was in physical pixels, so absolute clicks
  landed ~25% off. Now `_ensure_windows_dpi_awareness()` runs in
  `ClawTouchMcpServer.__init__` regardless of how `--screen` was
  resolved, keeping `cursor.py` and the clamp in the same
  coordinate space.

**P1 — server, bridge, CLI, examples**

- **`--screen` validation:** `0x0`, negative values, and malformed
  strings (`"1920x"`, `"1x2x3"`) used to either silently disable
  clamping (zero is falsy) or crash with an unhandled
  `ValueError`. Now `__main__.py` rejects all three with a clear
  `parser.error` message.
- **`--ops-per-sec` validation:** `0` or negative bricked every
  tool call (`initialize` and `tools/list` worked, every
  `tools/call` raised "rate limit exceeded"). Now rejected at the
  CLI with `parser.error`.
- **`hid.screenshot` region clamp + size cap:** an agent-supplied
  `region=[x1,y1,x2,y2]` with negative offsets or huge sizes used to
  capture across monitors the user may not have intended to expose,
  and a 4K×4K PNG (~30-80 MB base64) routinely OOMed the MCP client's
  JSON-RPC buffer. Now region is clamped to the primary monitor's
  bounds before grabbing, and `width × height > MAX_SCREENSHOT_PIXELS`
  (4M) returns a clear `ValueError` tool error.
- **`shutdown` method actually stops the server.** It used to return
  `{}` but never set `_stopping`, never closed the bridge, never
  broke `run_stdio`; clients saw the ack and stopped reading stdout,
  leaving the server blocked writing into a closed pipe. Now:
  `dispatch` sets `self._stop_event`, `run_stdio` checks the event
  on every loop turn and exits cleanly. Also handles
  `notifications/exit` for clients that prefer that path, and
  `notifications/cancelled` no-op so it's not flagged as unknown
  method.
- **Bridge IO failures now carry a diagnostic.** `_read_one_frame`
  used to return `None` for every failure (timeout / short header /
  short payload / parse error / mismatched seq) and the wrapping
  `mouse_*` / `key_*` methods returned a bare `ok=False` — the
  agent had no signal whether to retry, re-init, or escalate. Now
  each failure path sets `bridge.last_error_detail` with the
  specific reason, and `_on_tool_call` pulls it into the `isError`
  payload. Same hook surfaces firmware ERROR-frame responses with
  their `ErrorCode` name (`UNKNOWN_COMMAND` / `INVALID_PAYLOAD` /
  `CHECKSUM_MISMATCH` / `EXECUTION_TIMEOUT` / `DEVICE_BUSY`)
  instead of opaque ok=False. New `BridgeError` / `BridgeAckTimeout`
  / `BridgeAckMismatch` / `BridgeProtocolError` /
  `BridgeErrorResponse` exception classes exported from
  `clawtouch_mcp.bridge` so external bridge consumers can `try/except`
  the strongly-typed failure modes too.
- **`seq_id` 16-bit wrap now skips 0.** After 65535 ops the counter
  used to wrap to 0, colliding with the protocol's default
  `seq_id=0` on any frame built without an explicit seq. Long-
  running MCP sessions could see a stale default-seq ACK match a
  fresh request after wrap. Now `_next_seq` skips 0 on wrap.
- **`hid.type` strips control characters by default** (`\n`, `\r`,
  `\t`, `\x00`-`\x1f`, `\x7f`). An LLM agent drafting a multi-line
  message into a chat input would otherwise have its draft
  accidentally submitted by the `\n` being typed as Enter on the
  host. Pass `allow_control=True` to opt in to the raw byte stream
  (e.g. when intentionally driving a terminal app). Counts of
  stripped chars are logged at INFO so users notice.
- **`examples/computer_use/claude_demo.py` thinking + max_tokens
  contradiction fixed.** `thinking={"type":"adaptive"} + max_tokens
  =4096` is rejected by the Anthropic SDK (adaptive thinking
  requires `max_tokens` higher than the implicit thinking budget).
  Bumped to 16384 with an inline comment explaining the coupling
  with `model` and the `betas=` string.
- **`examples/computer_use/openai_cua_demo.py` scroll direction
  fixed.** The ternary `-(dy // 10) if dy > 0 else -(dy // 10)` had
  identical branches (never flipped sign) and Python's floor
  division of negatives over-scrolled upward (`-15 // 10 == -2`,
  not `-1`). Now `int(-dy / 10)` — single expression, correct
  rounding for both signs.
- **Dead `import io` removed from `claude_demo.py`.**
- **`examples/computer_use/README.md` `--ops-per-sec` line corrected.**
  Demo talks to `SerialHidBridge` directly (NOT through the MCP
  server), so the server's rate limiter is not in the loop —
  previous "default 10 in these demos" was simply wrong. README
  now says "pace tool calls yourself; add `asyncio.sleep` /
  `asyncio.Semaphore` if you need a cap".
- **Cross-repo wire protocol byte-equality test added.**
  `tests/test_cross_repo_protocol.py` (22 tests) compares every
  builder + enum value between `clawtouch_mcp.protocol` (this repo)
  and `clawtouch_hid_protocol.protocol` (the firmware repo) — the
  two packages independently implement the same frozen v1.0 wire
  format, and nothing else guards against silent drift. Skipped
  via `pytest.importorskip` when `clawtouch-hid-protocol` is not
  installed.
- **Test `_run(coro)` helpers no longer leak event loops.** Three
  test files used `asyncio.get_event_loop_policy().new_event_loop()
  .run_until_complete(coro)` and never closed the loop, producing
  ResourceWarning on Windows + orphan idle-watch tasks between
  tests. Now `try/finally` close.

**Tests:** 102 → **124** (added 16 cursor + 6 keycodes regression
guards in earlier commits, plus updated 2 dispatch tests for the new
`isError` contract). Cross-repo test suite adds another **22** when
`clawtouch-hid-protocol` is installed in dev mode.

### Fixed — absolute-coordinate semantics (codex round 3 P0/P1 #1)

- **`hid.click(x, y)` / `hid.move(x, y)` were not absolute.** Before
  this commit, `_tool_click` sent the raw target `(x, y)` to the
  firmware as a MOUSE_MOVE with `relative=False` flag set; the
  firmware's `_handle_mouse_move` ignored the flag entirely (USB HID
  Boot Mouse has no absolute-coordinate report) and treated `(x, y)`
  as a relative delta. An agent calling `hid.click(500, 300)` would
  see the cursor jump 500 px right and 300 px down from its current
  position, not land at the absolute (500, 300). Any Computer Use
  loop driving Claude Desktop / Cursor / Cline through this server
  would have mis-clicked on every call.

  **Fix architecture:** absolute coordinate semantics now live where
  they belong — on the host, not the firmware. New module
  `clawtouch_mcp/cursor.py` queries the OS for the current cursor
  position via `ctypes`:
    - Windows → `user32.GetCursorPos`
    - macOS   → `CoreGraphics.CGEventGetLocation` (via ctypes — no
      pyobjc dep)
    - Linux/X11 → `libX11.XQueryPointer`
    - Linux/Wayland → unsupported (no public unprivileged API);
      returns None deliberately
  `_tool_click` / `_tool_move` / `_tool_hover` now compute `(dx, dy)
  = (target - cursor)` and send a *relative* move that the firmware
  can actually execute. The firmware code path is unchanged and is
  now correctly documented as relative-only.

  **Failure path:** when the OS cursor query is unavailable (Wayland,
  unloadable libX11, GetCursorPos failure), the tool returns a
  structured error containing the platform-specific reason and the
  `relative=true` workaround — agents get a clear actionable message,
  not silent mis-clicks.

  **New `relative` parameter on `hid.click` / `hid.move`** lets an
  agent bypass the OS cursor query entirely and send raw pixel deltas
  — useful for headless / Wayland hosts and for sub-pixel scroll-like
  motion.

  **Test hook:** `CLAWTOUCH_FAKE_CURSOR=x,y` env var bypasses the OS
  query and returns the parsed coordinates instead, used by
  `tests/conftest.py` so the suite runs deterministically on headless
  CI without an X display.

  **Coverage:** new `tests/test_cursor.py` (16 tests) locks the env
  hook semantics, the delta math, the missing-cursor error path, the
  `relative=true` fast path, and `hid.move` / `hid.hover` parity.
  Total mcp test count: 118 (was 102).

  **README + tool descriptions** updated to spell out: default
  absolute via OS cursor query, `relative=true` opt-out, Wayland
  caveat, and the firmware-is-relative-only invariant.

### Fixed — second-pass code audit (codex round 3)

- **`examples/computer_use/claude_demo.py` — `ctrl+l` typed as bare
  'l' instead of triggering shortcut.** The "key" action fall-back
  used `key_name if not mods else key_name` (both branches identical),
  so any single-character key name with modifiers silently went to
  `bridge.type_text()` and missed the shortcut. Now only fall-back to
  `type_text` when there are no modifiers; with modifiers route to
  `bridge.key_combo(mods, key_name)` which can translate printable
  chars to keycodes, with a graceful `ValueError` catch for truly
  unknown keys.
- **`keycodes.py` missing punctuation-name aliases** like `plus`,
  `equal`, `minus`, `comma`, `period`, etc. — skill files (e.g.
  `clawtouch-skills/wps-office.md`) using
  `hid.key("ctrl+shift+plus")` would raise `ValueError unknown key:
  'plus'`. Added the common worded aliases so skills can reference
  punctuation by name; existing `=` / `+` literal usage still works.

### Terminology

- **Outward-facing copy: "LLM agent" → "AI agent"** in README hero,
  hero SVG alt text + diagram comment, `## What is this?`,
  Scope · Accessibility use case, `## About`, the Computer Use
  example README, and this changelog's own diagram description.
  Tracks the broader 2025 industry shift (Anthropic / OpenAI /
  Cursor / Cline now all default to "AI agent" in their public
  docs), and is what HN / GitHub / VC / B2B audiences search for.
- **Technical / compliance copy unchanged.** "LLM agent" is
  retained in: the `## Content generation` and `## Acceptable use`
  sections (legal precision — the LLM is the AI-content-generating
  party, not "any AI"), `SECURITY.md` (security-policy precision),
  `pyproject.toml` keyword comment (maintainer note), and the
  `clawtouch-skills` cross-link row on the Open source roadmap
  (matches the skills repo's internal wording, since markdown
  skills are LLM-specific by design — non-LLM agents have no use
  for prose prompts).

### Docs trim

- Removed redundant `🌐 clawtouch.cn` top-of-README link line —
  felt out-of-place above the badges (the same link still lives in
  the `## About` and `## License` sections).
- Removed the "🎥 a real screen-recording GIF will land here..."
  placeholder under `## See it in action`. The annotated stdio
  transcript stands on its own; no GIF promise to deliver on.
- Removed the "The dates aren't fixed — we ship when each piece is
  properly polished. Star the org..." sentence under
  `## Open source roadmap` — pure boilerplate, no information value.

### Visual / docs uplift

- **`docs/assets/hero.svg`** — flat-design hero diagram (AI agent →
  clawtouch-mcp → Pico 2 → target OS) embedded at the top of the
  English and Chinese READMEs. Highlights `clawtouch-mcp` as the
  "this repo" node and labels each transport hop (MCP stdio JSON-RPC
  / USB-CDC v1.0 frames / USB HID reports).
- **Architecture overview converted to Mermaid.** The previous
  ASCII box diagram in `## Architecture overview` is now a Mermaid
  `flowchart LR` with the `clawtouch-mcp` node highlighted (amber
  fill / thick border) as the this-repo marker. Renders natively on
  GitHub.
- **New `## See it in action` section.** An annotated stdio
  JSON-RPC transcript showing the full MCP `initialize` →
  `tools/list` → `tools/call` flow, with one `hid.click` and one
  `hid.type` call against a real Pico 2. Captured from
  `--log-level INFO` (USB serial randomized in the transcript). Acts
  as a text-based demo until a real screen-recording GIF lands.

### Compliance — second-pass audit (codex round 2)

A follow-up codex audit on the first compliance pass surfaced six
issues, all fixed below. The compliance scope is unchanged; wording
and packaging metadata are now stricter:

- **`## Acceptable use` reworded to scope-of-support, not a use
  restriction.** Replaced "you may not configure it to" with "this
  project does not support, document, or assist with". Added an
  explicit sentence that the section describes maintainer support
  scope only and is **not** an additional restriction on top of the
  MIT License's grant of code-level rights. Avoids the "MIT + use
  ban" structural conflict.
- **PRC Anti-Unfair Competition Law Art. 13 dating corrected.**
  Was "as amended 2025-10-15", which conflates promulgation and
  effective dates. Now reads "promulgated 2025-06-27, effective
  2025-10-15" (the latter is when the amendment takes effect, per
  the SPC publication). The substantive description was also
  broadened from the narrow "improper acquisition of others' data"
  to the statutory phrasing covering circumvention of technical
  management measures, fraud, and coercion as means.
- **`pyproject.toml` upgraded to PEP 639 license metadata.** Replaced
  `license = { text = "MIT" }` (deprecated table form) with
  `license = "MIT"` (SPDX expression). Added `license-files =
  ["LICENSE", "LICENSE.zh-CN.md", "NOTICE", "TRADEMARKS.md"]` so all
  four legal documents ship in the PyPI sdist/wheel `.dist-info/`
  directory. Bumped `setuptools>=77` (PEP 639 baseline). Removed the
  legacy `License :: OSI Approved :: MIT License` classifier per
  PyPA's PEP 639 migration guidance.
- **TRADEMARKS — owned-mark policy reworded to separate copyright
  and trademark grants.** The previous "non-commercial
  interoperability only" wording was ambiguous and could be read as
  restricting commercial use of the MIT-licensed code. Now states
  explicitly that MIT grants full commercial rights to the source
  code, that the marks are governed separately by trademark law,
  and that the only practical constraint on commercial forks is the
  trademark / naming requirement (rename, do not imply endorsement).
- **TRADEMARKS — official mark-owner attribution statements added.**
  New `### Official trademark attribution` subsection cites the
  attribution wording requested by Raspberry Pi Ltd., Adafruit
  Industries, Microsoft, Apple, Anthropic, and OpenAI per their
  respective trademark policies. Bilingual (English + 简体中文).

### Added
  and third-party marks referenced for descriptive purposes
  (Claude, OpenAI, Cursor, OpenClaw, Hermes, Cherry Studio, Trae IDE,
  Raspberry Pi, CircuitPython, Windows, macOS, etc.). PRC Trademark
  Law Art. 59 nominative-fair-use disclaimer included.
- **`LICENSE.zh-CN.md`** — non-official Chinese translation of the
  MIT License with explicit "English version prevails in case of
  conflict" disclaimer; references PRC open-source contract-law
  precedents (数字天堂诉柚子科技 / 罗盒诉风灵).
- **README `## Content generation — out of scope` section** —
  explicit declaration that this package does not generate any
  text/image/audio/video content, separating compliance scope from
  PRC *AI Generated Content Labeling Measure* (effective 2025-09-01)
  and the *Interim Measures for Generative AI Services*.
- **README `## Acceptable use` section** — explicit prohibition on
  bypassing target platforms' anti-fraud / risk-control / rate-limit
  measures and on operating accounts the user does not lawfully
  own; references PRC *Anti-Unfair Competition Law* Art. 13 (as
  amended 2025-10-15).
- **README License section** — added cross-links to
  `LICENSE.zh-CN.md`, `NOTICE`, and `TRADEMARKS.md`; clarified that
  MIT does not grant trademark rights.

### Changed

- **Test fixture USB serial numbers replaced with a synthetic value**
  (`E660000000000000`) instead of a real test-device serial; the
  affected test docstring rewritten to describe the technical
  reproduction scenario in neutral terms (no internal-date
  references).

### Fixed

- **`build_key_release()` now sends `[0x00, 0x00]` payload** instead of
  empty payload. Firmware `_handle_key_release` rejects frames with
  `len(payload) < 2` as `ERR_INVALID_PAYLOAD`, so `release_all()` was
  100% failing on real hardware. Spec ([protocol-v1.md §3.3](https://github.com/tinqiao-oss/clawtouch-hid/blob/master/docs/protocol-v1.md))
  says all-zero payload = release-all; the SDK now matches.
- `build_key_release()` gained optional `(keycode, modifiers)` params
  so a single key can be released too (backwards-compatible — bridge
  callers using `release_all()` keep working unchanged).
- Two new round-trip tests in `tests/test_protocol.py` lock the
  keyboard-payload byte order so it can't silently regress. (Historical
  note: at 0.2.4 that order was `keycode`-first; it was later unified to
  `[modifiers, keycode]` in protocol 1.1.1 — see the [1.1.1] entry.)

### Changed

- **Docs / scope wording softened.** Rewrote scope paragraphs in both
  READMEs and `CONTRIBUTING.md` to describe HID input neutrally —
  driver-stack routing, no software on target. `docs/windows-setup.md`
  scope section trimmed for the same reason.
- `_detect_screen()` docstring + this changelog fixed to reflect that
  v0.2.3 actually uses `SM_CXSCREEN` / `SM_CYSCREEN` (primary monitor),
  not the `*VIRTUALSCREEN` variants — the docstring was wrong, the
  code was right.

### Removed

- `bridge._PICO_PIDS` constant — dead code. `likely_pico` detection
  only checks VID (line 89), the PID set was never read. Verified by
  grep + live `python -c "list_pico_ports()"` on a real Pico 2
  (which is PID `0x000B`, not in the old set, and was correctly
  flagged as `likely_pico=True` anyway). Both setup docs updated
  to stop referencing the removed constant.

## [0.2.3] — 2026-05-17 — Screen auto-detect + Windows setup guide

### Added

- **Auto-detect primary monitor's physical pixel size on startup** when
  `--screen` is not passed. Coordinates clamp to the real screen
  instead of the user having to guess. Implementation:
  - Windows: `ctypes.windll.user32.GetSystemMetrics(SM_CXSCREEN
    / SM_CYSCREEN)` (primary monitor) after `SetProcessDpiAwareness(2)`
    (or v1 fallback on pre-1809), so detection returns **physical**
    pixels regardless of display scaling.
  - macOS / Linux: `tkinter` (standard library — no extra dep).
  - All paths fail soft. If detection fails, the server logs a warning
    and runs with no clamping, same as if `--screen` was omitted
    pre-0.2.3.
- **`device.info` returns a new `screen` field** with `width`, `height`,
  and `source` (`"explicit"` / `"detected"` / `"unset"`). An MCP client
  can read this to know the active clamp bounds at runtime — no more
  guessing whether the agent's coordinate system matches the server's.
- **`docs/windows-setup.md`** (~250 lines) covers: VS Code Claude
  extension `.mcp.json` scope (NOT `~/.claude.json` top-level — the
  extension doesn't read it), full-window-restart requirement, dual
  COM port enumeration (VID `2E8A` PID `000B`), display-scaling and
  HID-coordinate relationship, multi-monitor `SM_CXSCREEN` (primary-
  only) semantics, and a real e2e Python script to validate end-to-end
  after install.
- 7 new tests in `tests/test_screen_detect.py` covering: explicit
  `--screen` beats detection / detection populates ServerConfig /
  detection failure → `source = "unset"` (no clamp) / partial-explicit
  still triggers detection / clamp uses detected bounds / no clamp when
  unset / real `_detect_screen()` returns `Optional[tuple[int,int]]`.
  Total test count: 68 (was 61).

### Changed

- `README.md` Run examples drop the hard-coded `--screen 1920x1080` —
  v0.2.3 doesn't need it. The README now points to both
  `docs/windows-setup.md` and `docs/macos-setup.md` upfront.

### Discovered

- Mismatched `--screen` is silent: a 5120×1440 super-wide screen with
  `--screen 1920x1080` clamps clicks to a 1920×1080 rectangle in the
  upper-left and **silently swallows** any click past those bounds.
  Found during Windows real-hardware bring-up of the Claude Code VS
  Code extension MCP integration. Auto-detect prevents this by default;
  agents can still pass `--screen` explicitly to clamp to a chosen
  monitor in multi-monitor setups.
- The VS Code Claude Code extension (2.1.143) reads `.mcp.json` at
  project root but **ignores `~/.claude.json` top-level `mcpServers`**
  even though the CLI honors it. This is documented in
  `docs/windows-setup.md` so future contributors don't repeat the same
  ~30-minute debug loop.

### Compatibility

- No breaking changes. Existing scripts that pass `--screen` continue
  to behave identically (explicit wins). Anyone that omitted `--screen`
  before now gets auto-clamp; pass `--screen` explicitly to force the
  old "no clamp at all" behavior is no longer possible without code
  changes — but it was never documented as intentional anyway, and
  auto-clamp is strictly safer.

## [0.2.2] — 2026-05-17 — Windows stdio asyncio P0 fix

### Fixed

- **Server completely unusable on Windows** in 0.2.0 / 0.2.1: the
  asyncio stdio reader used `loop.connect_read_pipe(sys.stdin)`, which
  the Windows `ProactorEventLoop` rejects (`CreateIoCompletionPort`
  refuses anonymous pipe handles → `OSError: [WinError 6]`). Any MCP
  client (Claude Desktop, Cursor, Cline, Claude Code, …) that spawned
  `clawtouch-mcp` on Windows hung the `initialize` handshake forever
  with no stdin processed and no useful error to the client. Discovered
  on Windows 11 Python 3.13 during MCP-client bring-up; not caught by
  mac/Linux validation because POSIX `SelectorEventLoop` supports
  `connect_read_pipe(stdin)`.
- `run_stdio` now reads stdin via `asyncio.to_thread(sys.stdin.buffer.readline)`
  on every platform — performance is fine for MCP traffic (single-digit
  req/s) and the code is now identical across OSes.

### Added

- `tests/test_stdio_integration.py` — 7 end-to-end stdio tests that
  spawn `python -m clawtouch_mcp --mock` as a real subprocess and
  exchange JSON-RPC over its pipes. The pre-0.2.2 unit tests all used
  the in-process `ClawTouchMcpServer` directly, so the stdio reader was
  never exercised under pytest — which is exactly why the Windows
  asyncio bug shipped. The new tests run on every platform in CI; the
  bug only reproduces on Windows but the regression guard is cheap.
  Total test count: 61 (was 54).

### Compatibility

- No API change. `auto_detect_port` / `SerialHidBridge` / wire protocol
  / config flags all unchanged from 0.2.1.
- No firmware update required.
- POSIX users see no behavior change — same JSON-RPC framing (line-
  delimited or `Content-Length`), same dispatch semantics. The internal
  reader switched from `asyncio.StreamReader` over a connected pipe to
  a thread-backed `readline`; user-visible behavior is identical.

## [0.2.1] — 2026-05-17 — Dual-CDC port detection fix

### Fixed

- **`auto_detect_port()` silently picked the REPL console instead of
  the data channel** on every Pico flashed with the standard ClawTouch
  firmware (`boot.py` enables `console=True, data=True`). The two CDC
  channels share VID/PID/serial_number, so the pre-0.2.1 logic
  returned whichever device pyserial listed first — typically the
  console — which then ignored every framed protocol byte and made
  `ping()` return `False` without an error. Discovered on a fresh
  Apple Silicon Mac mini during macOS bring-up (cu.usbmodem21201 vs
  21203).
- `_port_sort_key` does **natural** numeric ordering on the trailing
  port number, so `COM10` correctly sorts after `COM3` on Windows
  (lexicographic would invert them and pick the console).

### Added

- `is_data_port` field on each `list_pico_ports()` entry — `True`
  only for the highest-numbered port within each shared-serial
  group. Single-CDC firmwares degrade gracefully (sole port is
  marked data).
- 11 new tests covering: macOS dual CDC (`cu.usbmodem*`), Windows
  dual COM with two-digit numbers, Linux dual `/dev/ttyACM*`, single
  CDC, two Picos with distinct serials, and mixed Pico + non-Pico
  enumeration. Total test count: 54 (was 43).

### Compatibility

- `auto_detect_port()` return value changes for users who previously
  worked around the bug by passing `--port` explicitly to the data
  channel — they can now drop the flag. Anyone who happened to depend
  on the old (broken) behavior must now explicitly pass the lower-
  numbered console port via `--port`.
- No firmware update required. No hardware update required. The bug
  was always host-side.

## [0.2.0] — 2026-05-17 — First public release

First public release of the MCP server. Earlier internal builds existed
under the working name `openclaw-mcp` but were never published. The
0.x line is **stable for the v1.0 wire protocol** and the MCP
2024-11-05 protocol revision.

### Added

- **10 MCP tools** mapping LLM tool calls to HID primitives:
  `hid.click` / `hid.move` / `hid.hover` / `hid.type` / `hid.scroll`
  / `hid.key` / `hid.release_all` / `hid.screenshot` (opt-in) /
  `device.list` / `device.info`.
- **MockBridge** (`--mock`) for hardware-free development and CI.
- **Auto-detection** of Raspberry Pi Pico 2 boards via USB VID/PID; or
  explicit `--port`.
- **Safety rails**: coordinates clamped to `--screen WxH`, typed text
  capped at 4096 chars per call, rate-limited via `--ops-per-sec`.
- **Stdio framing** auto-detection (Content-Length vs. line-delimited
  JSON) — works with Claude Desktop, Cline, Continue, Cursor,
  [OpenClaw](https://github.com/openclaw/openclaw), and
  [Hermes Agent](https://github.com/NousResearch/hermes-agent) out of
  the box.
- Test suite: 43 tests cover protocol round-trip, keycode mapping,
  dispatcher, rate limiter, and coordinate clamping.

### Known limitations

- Keyboard layout assumes US ABC. Hosts with a different system input
  method may see typed characters render as the wrong glyph; use
  `hid.key` for navigation and `hid.type` only on US-layout hosts.
- USB-CDC serial transport only; wireless transports are out of scope
  for this OSS release.
- No multi-touch HID profile yet — only mouse and keyboard.

[Unreleased]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.4.6...v0.5.0
[0.4.6]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.4.3...v0.4.5
[0.4.3]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.3.2...v0.4.1
[0.3.2]: https://github.com/tinqiao-oss/clawtouch-mcp/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/tinqiao-oss/clawtouch-mcp/releases/tag/v0.3.1
