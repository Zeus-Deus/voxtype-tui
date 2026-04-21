# voxtype-tui

A Textual-based TUI for managing [Voxtype](https://voxtype.io) — a push-to-talk voice-to-text daemon for Linux. Voxtype is configured through a single `~/.config/voxtype/config.toml` file; this TUI gives you an ergonomic frontend for the parts you're most likely to edit interactively: vocabulary (words injected into Whisper's prompt), a post-transcription replacement dictionary with category tags, all the usual engine / hotkey / audio / output / VAD / GPU settings, and a Models tab that lists, downloads, deletes, and activates model files.

## Install

Miniconda is recommended so the Textual/tomlkit versions stay pinned away from your system Python.

```bash
# Create the env (Python 3.12)
conda create -n voxtype-tui python=3.12 pip -y
conda activate voxtype-tui

# Install runtime deps + the package itself in editable mode
pip install -r requirements.txt
pip install -e .
```

`pip install -e .` installs the `voxtype-tui` console entry point into the active env. You can invoke either:

```bash
python -m voxtype_tui
# or
voxtype-tui   # from inside the activated env
```

## Omarchy integration (optional)

If you're running [Omarchy](https://omarchy.org), two scripts wire up a global keybinding and a floating-window rule so the TUI opens as a centered 1100×750 dialog.

```bash
bash scripts/install-omarchy.sh     # default keybind: SUPER CTRL ALT, X
bash scripts/uninstall-omarchy.sh   # removes the managed lines + wrapper
```

What it does:

- Drops a launcher wrapper at `~/.local/bin/voxtype-tui` that activates the conda env and runs `python -m voxtype_tui`. (The wrapper tries `~/miniconda3`, `~/anaconda3`, `/opt/miniconda3` in that order.)
- Appends a `windowrule` to `~/.config/hypr/windows.conf` matching `org.omarchy.voxtype-tui` (float, center, size 1100×750).
- Appends a `bindd` to `~/.config/hypr/bindings.conf` firing `omarchy-launch-or-focus-tui voxtype-tui` on `SUPER CTRL ALT, X`.
- Each edit is wrapped with a sentinel comment (`# voxtype-tui-managed (do not edit this line manually)`) so uninstall can find them. Re-runs are safe — the script checks for the sentinel and skips if present.
- Runs `hyprctl reload` at the end.

**Pre-install guardrails:** the script refuses if `~/.config/omarchy/` doesn't exist (non-Omarchy system — just use `python -m voxtype_tui`) or if `SUPER CTRL ALT, X` is already bound to something else. To use a different keybinding, edit `BIND_KEY` at the top of `install-omarchy.sh`.

### Omarchy key namespace cheatsheet

As of Omarchy 3.5.x, these `SUPER+CTRL` and `SUPER+CTRL+ALT` combinations are already in use — pick an unused one if `SUPER CTRL ALT, X` conflicts with anything you've added.

| Modifier | Taken by Omarchy defaults |
|---|---|
| `SUPER+CTRL` | `A`, `B`, `C`, `E`, `F`, `I`, `L`, `N`, `O`, `S`, `T`, `V`, `W`, `X`, `Z`, `SPACE`, `COMMA`, `BACKSPACE`, `TAB`, `DELETE`, `LEFT`, `RIGHT` (plus `U` for codexbar if installed) |
| `SUPER+CTRL+ALT` | `T` (time), `B` (battery), `Z` (reset zoom) — everything else free |

## Keybindings (in-TUI)

### App-level

| Key | Action |
|---|---|
| `1` / `2` / `3` / `4` | Switch to Vocabulary / Dictionary / Settings / Models |
| `ctrl+s` | Save — pre-validates via `voxtype -c <tmp> config`, writes atomically, prompts for daemon restart if a restart-sensitive field changed |
| `ctrl+r` | Reload config from disk (refuses if unsaved changes exist) |
| `ctrl+p` | Command palette — includes the theme picker |
| `ctrl+q` | Quit (confirms if unsaved changes exist) |

Digit keys and letter shortcuts yield to Input widgets automatically — typing `"kubectl"` in an Input inserts the characters, it doesn't jump tabs.

### Vocabulary / Dictionary / Models tabs

Vim-flavored table navigation plus per-tab actions.

| Key | Action |
|---|---|
| `j` / `k` | Cursor down / up |
| `g` `g` | Jump to top of the table (two quick presses within 500 ms) |
| `G` | Jump to bottom |
| `n` | Context-sensitive: focus the Add input when search is empty, next match when a search filter is active |
| `N` | Previous search match |
| `/` | Focus the search input |
| `d` | Delete the selected row (toast + `u` to undo on Vocabulary / Dictionary) |
| `u` | Undo last delete (Vocabulary / Dictionary) |
| `c` | Cycle category on the selected row (Dictionary only — sidecar-only change) |
| `r` | Refresh disk scan (Models tab) |
| `escape` | Blur an Input, return focus to the table |

Mouse clicks work too — click a row to select, click any button to activate.

## What lives where

### `~/.config/voxtype/config.toml` — source of truth (owned by Voxtype)

Everything that maps to Voxtype itself — vocabulary phrases, replacement maps, engine / model / hotkey / audio / output / VAD / post-process / remote-backend settings — round-trips through this file. The TUI uses [tomlkit](https://github.com/python-poetry/tomlkit) so every write preserves comments and formatting. Saves are atomic (tempfile + `os.replace`) and pre-validated by running `voxtype -c <tmp> config` before the swap — if Voxtype's parser rejects the output, the save aborts and your original file is untouched. The first write to any config drops a one-time rescue copy at `<path>.voxtype-tui-bak`.

### `~/.config/voxtype-tui/metadata.json` — sidecar (owned by voxtype-tui)

UI-only annotations that don't belong in Voxtype's config: vocabulary entries as structured objects (so the UI can show a list rather than parsing the comma-joined string on every render), per-replacement category tags (Replacement / Command / Capitalization), and `added_at` timestamps. On load the sidecar is reconciled against the config. If you hand-edit `config.toml` and the sidecar diverges, **the config wins** — the sidecar is rebuilt from disk and a dismissible warning banner surfaces the divergence at the top of the affected tab.

## Restart-sensitive fields

Some Voxtype fields are baked in at daemon startup and need `systemctl --user restart voxtype` to take effect:

- `state_file`, `engine`
- `whisper.{model, mode, backend, gpu_isolation, remote_endpoint}`
- `parakeet.model_type`, `moonshine.model`, `sensevoice.model`, `paraformer.model`, `dolphin.model`, `omnilingual.model`
- `hotkey.{key, modifiers, mode, enabled}`
- `audio.{device, sample_rate}` and `audio.feedback.*`
- `vad.{enabled, model, threshold}`

Everything else (initial_prompt, replacements, language, output mode, spoken punctuation, etc.) is re-read per-transcription and takes effect on the next dictation without a restart. The TUI prompts to restart only when one of these fields actually changed in the current save AND `systemctl --user is-active voxtype` reports `active`. It never restarts automatically — you decide when that happens.

## Theme

The TUI auto-detects Omarchy's active theme by reading `~/.config/omarchy/current/theme/alacritty.toml` and registers a `omarchy-auto` Textual theme built from it (accent = normal.yellow or bright.yellow, primary = normal.red or bright.red, foreground/background from the primary section). If Omarchy isn't detected, the default Textual theme is used.

**Override with your own colors:** drop `~/.config/voxtype-tui/theme.toml` with flat keys:

```toml
primary    = "#BF616A"
accent     = "#EBCB8B"
foreground = "#D8DEE9"
background = "#2E3440"
```

Both `#RRGGBB` and `0xRRGGBB` are accepted. The resulting `"user"` theme shows up alongside Omarchy and built-in themes in the `ctrl+p` command palette. Your selection is persisted to `~/.config/voxtype-tui/ui.json` and survives restarts. Omarchy theme *changes* require an app restart — live reload is deliberately not supported in v1.

## Tests

```bash
python -m pytest tests/
```

Six TOML fixture configs under `tests/fixtures/` cover representative real-world shapes (stock / heavily customized / externally edited / minimal / unusual whitespace / mostly-commented). Each mutation is parametrized across fixtures and asserts no data loss, preserved comments, touched-scope bounded to intended fields, and post-save validity per `voxtype -c <file> config`. UI behavior is verified with Textual's `Pilot` against tempdir config + sidecar copies — no real writes to `~/.config/voxtype/` happen during tests. The Omarchy install/uninstall scripts have their own bash-level tests that run against a sandboxed `$HOME`.

## Philosophy

- **Don't fork Voxtype.** Use it as-is through its config file.
- **Don't reimplement Whisper / STT.** That's Voxtype's job.
- **Don't write destructively.** Always read → merge → write, preserving comments, atomic swap, pre-validated.
- **Don't auto-restart the daemon.** You decide when.
- **`config.toml` is source of truth.** If the TUI's sidecar view diverges, the config wins and the UI warns.

See [`CLAUDE.md`](./CLAUDE.md) for deeper design notes.

## License

MIT
