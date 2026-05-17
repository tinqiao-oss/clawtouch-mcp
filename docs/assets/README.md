# docs/assets — screenshot drop folder

This folder holds image assets referenced by [`COMMERCIAL_PRODUCT.md`](../COMMERCIAL_PRODUCT.md)
(and any future docs that need screenshots / GIFs).

## Naming convention

- **All-lowercase `kebab-case.ext`** — e.g. `desktop-main.png`,
  `vision-detection.png`. No spaces, no underscores, no camelCase.
- **Format**: PNG for static screenshots (lossless, fine for UI),
  GIF for short animations (< 5 MB), WebM for longer clips.
- **Resolution**: at least 1280 px wide (Retina-friendly).
- **File size**: keep PNGs under ~300 KB — run them through
  [tinypng.com](https://tinypng.com/) or [squoosh.app](https://squoosh.app/)
  before committing. Anything > 500 KB will slow GitHub README rendering.
- **Locale**: the desktop product UI is currently Chinese-only, so all
  screenshots will be `zh-CN` UI. No locale suffix needed today —
  add `.en.png` / `.zh.png` later if an English UI ships.

## Expected screenshots (TODO)

These are the four placeholders referenced by `COMMERCIAL_PRODUCT.md`.
File names below are what the doc expects — keep them exact.

| File                       | Section | What to capture |
|----------------------------|---------|-----------------|
| `hosting-live.png`         | hero image at top of doc + §1 (24/7 hosted operation) | The hosting page in live state — current device status indicator, recent activity log scrolling, ideally with at least one active session. This is what users see most of the day; sells the "it actually runs unattended" point. |
| `persona-knowledge.png`    | §2 Persistent personas + knowledge base | The persona management page (or the persona + knowledge pages side by side if the layout permits). Show the list of personas + at least one persona detail, and the knowledge base listing in the same shot. |
| `adapters.png`             | §3 Platform adapters | The adapters / platforms page showing the supported app list (WeChat front-and-center, browsers, plus whatever else is shipped — emphasis on "already done" rather than "configurable"). |
| `workflow-editor.png`      | §4 Visual workflow editor + task templates | The task-template workflow editor mid-edit, with multiple steps visible in the canvas. Goal: show that this is a real drag-style editor, not a YAML file. |

The remaining four sections in `COMMERCIAL_PRODUCT.md` (scheduled
tasks, action system, mini-program remote management, full task
journal) are intentionally **text-only** — they describe capabilities
that either have no dedicated UI (action system) or whose UI would
add little to the doc beyond words (scheduler config, journal log,
miniprogram screen).

## Workflow

1. Take the screenshot in the desktop product (or trim from a longer
   recording for the GIF case).
2. Crop tightly to remove window chrome / desktop background unless
   it's part of the point.
3. Compress through tinypng / squoosh.
4. Drop into this folder with the exact file name from the table above.
5. Open `COMMERCIAL_PRODUCT.md`, find the matching `<!-- TODO: ... -->`
   comment, replace with:
   ```markdown
   ![Brief alt text](./assets/desktop-main.png)
   ```
6. Commit. CI doesn't gate image content, but the `<!-- TODO -->`
   comments are a visible signal that the doc isn't finished — once
   replaced, the doc reads as complete.

## Privacy / safety reminder

Screenshots can leak: open browser tabs in the taskbar, personal
filenames in window titles, WeChat contact names in adapter examples,
account info in status bars. Before committing, do a quick "what's in
the corners and edges" pass — anything sensitive either crop out or
blur (Snipping Tool's marker / GIMP rectangle select + Gaussian blur
both work).
