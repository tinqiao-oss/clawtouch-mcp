# DeepSeek Harness adapter

[`plugin/`](plugin/) is **`dsh-clawtouch`**, a [dsh](https://github.com/deepseek-ai/deepseek-harness)
plugin that turns this repo's HID tools into one natural-language call:

```
computer_click({ target: "the blue Send button at the bottom right" })
```

It lives in this repo rather than a separate one because it depends on
capabilities that ship here — `screen.windows`, and `hid.screenshot`'s
`markers` / `max_width` — and versioning them apart would mean a plugin
release that silently needs a `clawtouch-mcp` nobody has yet.

**The split is deliberate.** `clawtouch-mcp` stays raw HID plumbing with no
LLM and no agent loop; the plugin is where the second model, the skill and
the guard live. See [plugin/README.md](plugin/README.md) for why each of
those three cannot live in the server.
