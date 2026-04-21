# voxtype-tui docs

Short, topic-focused notes on what each setting does and when you'd want to touch it.

## The four tabs at a glance

| Tab | What it edits | When to open it |
|---|---|---|
| [Vocabulary](./vocabulary.md) | `[whisper] initial_prompt` | You keep saying project / product names that Whisper mis-transcribes |
| [Dictionary](./dictionary.md) | `[text] replacements` | You want "cloud code" → "Claude Code" every time, or spoken slash-commands |
| [Settings](./settings.md) | Engine, hotkey, audio, output, VAD, post-process, remote, GPU | Initial setup + tuning |
| [Models](./models.md) | Downloading / deleting model files, picking the active one | Switching between tiny / base / large, disk hygiene |

## Cross-cutting references

- [Ownership model](./ownership-model.md) — what lives in `config.toml` vs the TUI's sidecar, and how the two stay in sync.
- [Restart-sensitive fields](./restart-sensitive.md) — which changes need a daemon restart, which don't, and why.
- [Theming](./theming.md) — Omarchy auto-detect, user overrides, the `ctrl+p` theme picker.
- [Keybindings reference](./keybindings.md) — app-level, vim navigation, per-tab actions.

## If you're brand new

1. Start on **Settings → Engine & Model** — pick your engine (probably whisper) and a model size. `tiny.en` is fastest, `base.en` is a good default, `large-v3-turbo` is the quality-at-speed sweet spot if you have a GPU.
2. **Settings → Hotkey** — pick the key you'll push-to-talk with. Voxtype ships with `SCROLLLOCK`; change it if your keyboard doesn't have one.
3. **Settings → Audio** — pick your microphone from the Device dropdown.
4. **Settings → Output** — `type` mode inserts the transcription where your cursor is. Turn on `auto_submit` if you're dictating into chat windows a lot.
5. Only after that: Vocabulary for proper nouns you care about, Dictionary for shortcuts.
