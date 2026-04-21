# Ownership model

Two files store voxtype-tui's state. Understanding which one owns what prevents confusion when something diverges.

## `~/.config/voxtype/config.toml` — owned by Voxtype

This is Voxtype's config file. Voxtype reads it; voxtype-tui writes it (carefully). Everything that actually affects how Voxtype transcribes — engine, model, hotkey, audio, output, VAD, post-process, remote, initial_prompt, replacements — lives here and only here.

voxtype-tui's writes are:

- **Comment-preserving** via [tomlkit](https://github.com/python-poetry/tomlkit). Hand-add a `# my note` next to a field; the TUI leaves it alone on subsequent saves.
- **Atomic** — writes go to a temp file in the same directory, then `os.replace`. No half-written file window.
- **Pre-validated** — before the temp file is swapped in, `voxtype -c <tmp> config` parses it. If voxtype's parser rejects it, the save aborts with an error toast and the original file is untouched.
- **Backed up once** — the first time voxtype-tui writes to a given `config.toml`, it drops a one-time rescue copy at `config.toml.voxtype-tui-bak` right next to it. Delete that file anytime; it won't be re-created.

## `~/.config/voxtype-tui/metadata.json` — owned by voxtype-tui

UI-only annotations that don't belong in voxtype's config:

- **Vocabulary** — each phrase stored as a structured entry (`phrase`, `added_at`, optional `notes`) rather than as opaque substrings of the comma-joined `initial_prompt` string.
- **Replacement categories** — your `Replacement` / `Command` / `Capitalization` tags for each entry in `[text].replacements`. Voxtype never sees these.
- **Timestamps** — when each vocab word / replacement was added.

None of this changes behavior in voxtype itself. It only makes the TUI's lists easier to reason about.

## `~/.config/voxtype-tui/ui.json` — UI preference

One-line file: `{"theme": "omarchy-auto"}` or similar. Remembers your theme pick across restarts. Separate from metadata.json so user data and UI preferences stay distinct.

## What happens when the two diverge

Hand-edit `config.toml` while the TUI is closed, or via the Omarchy waybar "right-click → edit config" flow. Next time the TUI opens, **config.toml wins**:

- The TUI's reconcile pass parses the current `initial_prompt` / `replacements` from config, compares against what the sidecar expects, and rebuilds the sidecar to match. Orphan sidecar entries (entries whose key is no longer in config) get dropped. New entries from the config get a default category tag.
- A **dismissible warning banner** appears at the top of the affected tab explaining what got rebuilt.

## Why two files?

Everything could live in `config.toml` — categories inlined as TOML comments, timestamps in subtables. Two reasons we didn't:

1. **Categories aren't real behavior.** Voxtype treats `[text].replacements` as a flat map. Putting category metadata in the same file would clutter it with fields voxtype ignores.
2. **Hand-editability.** People edit `config.toml` directly. The TUI shouldn't bloat it with JSON-ish metadata that only the TUI understands. Keep voxtype's file lean; keep our metadata beside it.
