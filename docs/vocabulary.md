# Vocabulary

A list of proper nouns and jargon that Whisper will lean toward recognizing. Stored as `[whisper] initial_prompt` in the config, shown in the TUI as a structured list (sidecar) so you can add / delete / search individual entries.

## What it actually does

Whisper supports an "initial prompt" — a short piece of context prepended to the audio transcription. The model uses it as a hint for which words are likely. It's a **bias, not a dictionary lookup** — if you say a word that's in your vocabulary, Whisper is more likely to transcribe it correctly, but it can still get it wrong, and words *not* in the list aren't rejected.

On disk:

```toml
[whisper]
initial_prompt = "Kubernetes, PostgreSQL, Whisper, TypeScript, Hyprland"
```

The TUI keeps each phrase as a structured entry in `~/.config/voxtype-tui/metadata.json` with a timestamp, then joins them with `", "` when writing back to `config.toml`.

## What to put in it

- **Product names** Whisper hasn't seen a lot of: `Omarchy`, `Hyprland`, `kubectl`.
- **Project-specific jargon**: your codebase's module names, your company's initialisms.
- **Proper names** with unusual spelling.

## What NOT to put in it

- Common English words — they're already in the model's vocabulary.
- Long sentences — the prompt has a ~224-token cap (you'll see the counter warn at 200, error at 224).
- Random noise just in case — more isn't better; too many irrelevant tokens can make Whisper mis-bias.

## Keybindings

- `n` — focus the Add input (or, if a search filter is active, jump to next match)
- `/` — focus the search input
- `d` — delete selected row (with undo toast)
- `u` — undo last delete
- `j` / `k` / `gg` / `G` — vim-style navigation

## Gotchas

- Changes take effect on the **next transcription**. No daemon restart needed.
- If you hand-edit `config.toml`'s `initial_prompt` directly, the TUI notices on next load, drops a reconcile warning banner, and rebuilds its sidecar list from what's on disk.
