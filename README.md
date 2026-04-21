# voxtype-tui

A Textual-based TUI for managing [Voxtype](https://voxtype.io) — a push-to-talk voice-to-text daemon for Linux. Voxtype is configured through a single `~/.config/voxtype/config.toml` file; this TUI gives you an ergonomic frontend for the parts you're most likely to edit interactively.

Four tabs:

- **Vocabulary** — words / proper nouns Whisper should lean toward (`[whisper] initial_prompt`).
- **Dictionary** — post-transcription text replacements with per-entry categories (Replacement / Command / Capitalization).
- **Settings** — engine, model, hotkey, audio, output, VAD, post-processing, remote backend, GPU acceleration.
- **Models** — list / download / delete / set-active for per-engine model files.

## Install

Uses miniconda for isolation.

```bash
# Create the env (Python 3.12)
conda create -n voxtype-tui python=3.12 pip -y
conda activate voxtype-tui

# Install deps
pip install -r requirements.txt
```

## Run

```bash
python -m voxtype_tui
```

## Keybindings

### App-level

| Key | Action |
|---|---|
| `1` / `2` / `3` / `4` | Switch to Vocabulary / Dictionary / Settings / Models |
| `ctrl+s` | Save — runs pre-save voxtype validation, writes atomically, prompts for daemon restart if a restart-sensitive field changed |
| `ctrl+r` | Reload config from disk (refuses if there are unsaved changes) |
| `ctrl+q` | Quit (confirms if unsaved changes exist) |

Digit keys and letter shortcuts automatically yield to Input widgets — typing `"kubectl"` into an Input inserts the characters, it doesn't try to jump tabs.

### Vocabulary / Dictionary / Models tabs

Vim-flavored table navigation plus per-tab actions.

| Key | Action |
|---|---|
| `j` / `k` | Cursor down / up |
| `g` `g` | Jump to top of table (two quick presses within 500 ms) |
| `G` | Jump to bottom |
| `n` | Context-sensitive: focus the Add input when search is empty, next match when a search filter is active |
| `N` | Previous match (search cycling) |
| `/` | Focus the search input |
| `d` | Delete the selected row (with toast + `u` to undo on Vocabulary/Dictionary) |
| `u` | Undo last delete (Vocabulary / Dictionary) |
| `c` | Cycle category on the selected row (Dictionary only — sidecar-only change) |
| `r` | Refresh disk scan (Models tab) |
| `escape` | Blur an Input, return focus to the table |

Mouse clicks work too — click a row to select, click any button to activate.

## What lives where

### `~/.config/voxtype/config.toml` — source of truth (owned by Voxtype)

Everything on this tab that maps to Voxtype itself — vocabulary phrases, replacement maps, model/engine/hotkey/audio/output/etc. settings — round-trips through this file. The TUI uses [tomlkit](https://github.com/python-poetry/tomlkit) so every write preserves comments and formatting. Saves are atomic (tempfile + `os.replace`) and pre-validated by running `voxtype -c <tmp> config` before the swap — if Voxtype's parser rejects the output, the save aborts and your original file is untouched.

On first write to a given config, a one-time rescue copy is dropped at `<path>.voxtype-tui-bak`.

### `~/.config/voxtype-tui/metadata.json` — sidecar (owned by voxtype-tui)

UI-only annotations that don't belong in Voxtype's config:

- Vocabulary entries as structured objects (so the UI can show a list rather than treating the comma-joined string as opaque).
- Per-replacement category tags (Replacement / Command / Capitalization) — on disk, Voxtype only sees one flat `[text].replacements` map; the category is our overlay.
- Timestamps (`added_at`) for both.

On load the sidecar is reconciled against the config. If you hand-edit `config.toml` and the sidecar diverges, the config wins: the sidecar is rebuilt from it and a dismissible banner warns you at the top of the affected tab.

## Restart-sensitive fields

Some Voxtype fields are baked in at daemon startup and need `systemctl --user restart voxtype` to take effect:

- `state_file`, `engine`
- `whisper.{model,mode,backend,gpu_isolation,remote_endpoint}`
- `parakeet.model_type`, `moonshine.model`, `sensevoice.model`, `paraformer.model`, `dolphin.model`, `omnilingual.model`
- `hotkey.{key,modifiers,mode,enabled}`
- `audio.{device,sample_rate}` and `audio.feedback.*`
- `vad.{enabled,model,threshold}`

Everything else (initial_prompt, replacements, language, output mode, spoken punctuation, etc.) is re-read per-transcription and takes effect on the next dictation without a restart.

The TUI prompts to restart only when one of these fields actually changed in the current save AND `systemctl --user is-active voxtype` reports `active`. It never restarts automatically — you decide when that happens.

## Running the tests

```bash
python -m pytest tests/
```

Six fixture configs under `tests/fixtures/` cover real-world shapes (stock, heavily customized, externally edited, minimal, unusual whitespace, mostly-commented). Each mutation is parametrized across fixtures and asserts no data loss, preserved comments, touched-scope bounded to intended fields, and post-save validity per `voxtype -c <file> config`.

UI behavior is verified with Textual's `Pilot` against tempdir config + sidecar copies — no real writes to `~/.config/voxtype/` happen during tests.

## Philosophy

- **Don't fork Voxtype.** Use it as-is through its config file.
- **Don't reimplement Whisper / STT.** That's Voxtype's job.
- **Don't write destructively.** Always read → merge → write, preserving comments, atomic swap.
- **Don't auto-restart the daemon.** You decide when.
- **`config.toml` is source of truth.** If the TUI's sidecar view diverges, the config wins.

See [`CLAUDE.md`](./CLAUDE.md) for deeper design notes.

## License

MIT
