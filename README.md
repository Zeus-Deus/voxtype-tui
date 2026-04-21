# voxtype-tui

A Textual-based TUI for managing [Voxtype](https://voxtype.io) — a push-to-talk voice-to-text daemon for Linux. Voxtype is configured through a single `~/.config/voxtype/config.toml` file; this TUI gives you an ergonomic frontend for the parts you're most likely to edit interactively: vocabulary (words injected into Whisper's prompt), a post-transcription replacement dictionary with category tags, all the usual engine / hotkey / audio / output / VAD / GPU settings, and a Models tab that lists, downloads, deletes, and activates model files.

## Install

```bash
yay -S voxtype-tui
```

Drops `/usr/bin/voxtype-tui`, `/usr/share/applications/voxtype-tui.desktop`, and the Omarchy integration scripts under `/usr/share/voxtype-tui/`. Pulls in `voxtype-bin`, `python-textual`, and `python-tomlkit` as dependencies.

### Run

```bash
voxtype-tui
# or
python -m voxtype_tui
```

## Omarchy integration (optional)

If you're running [Omarchy](https://omarchy.org), two scripts wire up a global keybinding and a floating-window rule so the TUI opens as a centered 1100×750 dialog.

```bash
bash /usr/share/voxtype-tui/install-omarchy.sh     # default keybind: SUPER CTRL ALT, X
bash /usr/share/voxtype-tui/uninstall-omarchy.sh   # removes the managed lines
```

What it does:

- Appends a `windowrule` to `~/.config/hypr/windows.conf` matching `org.omarchy.voxtype-tui` (float, center, size 1100×750).
- Appends a `bindd` to `~/.config/hypr/bindings.conf` firing `omarchy-launch-or-focus-tui voxtype-tui` on `SUPER CTRL ALT, X`.
- Each edit is wrapped with a sentinel comment (`# voxtype-tui-managed (do not edit this line manually)`) so uninstall can find them. Re-runs are safe — the script checks for the sentinel and skips if present.
- Runs `hyprctl reload` at the end.

**Pre-install guardrails:** the script refuses if `voxtype-tui` isn't on `$PATH` (install it via `yay -S voxtype-tui` first), if `~/.config/omarchy/` doesn't exist, or if `SUPER CTRL ALT, X` is already bound to something else. To use a different keybinding, edit `BIND_KEY` at the top of `install-omarchy.sh`.

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

UI-only annotations that don't belong in Voxtype's config: vocabulary entries as structured objects (so the UI can show a list rather than parsing the comma-joined string on every render), per-replacement category tags (Replacement / Capitalization), and `added_at` timestamps. On load the sidecar is reconciled against the config. If you hand-edit `config.toml` and the sidecar diverges, **the config wins** — the sidecar is rebuilt from disk and a dismissible warning banner surfaces the divergence at the top of the affected tab.

## Restart-sensitive fields

Some Voxtype fields are baked in at daemon startup and need `systemctl --user restart voxtype` to take effect:

- `state_file`, `engine`
- `whisper.{model, mode, backend, gpu_isolation, remote_endpoint}`
- `parakeet.model_type`, `moonshine.model`, `sensevoice.model`, `paraformer.model`, `dolphin.model`, `omnilingual.model`
- `hotkey.{key, modifiers, mode, enabled}`
- `audio.{device, sample_rate}` and `audio.feedback.*`
- `vad.{enabled, model, threshold}`

- `whisper.initial_prompt` (your Vocabulary)
- `text.replacements` (your Dictionary), `text.spoken_punctuation`, `text.smart_auto_submit`

The TUI prompts to restart whenever any of these actually changed AND `systemctl --user is-active voxtype` reports `active`. After the first modal in a session, a persistent pill — `⚠ Daemon restart needed (Ctrl+Shift+R)` — lives in the header until you restart the daemon, so subsequent edits don't spam modals but you never lose the signal that your daemon is stale. Click the pill or press `ctrl+shift+r` from anywhere to restart. It never restarts automatically — you decide when.

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
pytest          # parallel by default via pytest-xdist; ~12s for the full suite
pytest -n 0     # force serial — easier to read output when debugging one test
```

Six TOML fixture configs under `tests/fixtures/` cover representative real-world shapes (stock / heavily customized / externally edited / minimal / unusual whitespace / mostly-commented). Each mutation is parametrized across fixtures and asserts no data loss, preserved comments, touched-scope bounded to intended fields, and post-save validity per `voxtype -c <file> config`. UI behavior is verified with Textual's `Pilot` against tempdir config + sidecar copies — no real writes to `~/.config/voxtype/` happen during tests. The Omarchy install/uninstall scripts have their own bash-level tests that run against a sandboxed `$HOME`.

## Development

```bash
# Clone + dev env (Python 3.12)
git clone https://github.com/Zeus-Deus/voxtype-tui
cd voxtype-tui
conda create -n voxtype-tui python=3.12 -y
conda activate voxtype-tui
pip install -e ".[dev]"

# Run tests
pytest
```

Conda is used only for local development isolation. Shipped installs go through AUR — `install-omarchy.sh` assumes `voxtype-tui` is already on PATH from a system-level install and does not touch your conda environment.

## Philosophy

- **Don't fork Voxtype.** Use it as-is through its config file.
- **Don't reimplement Whisper / STT.** That's Voxtype's job.
- **Don't write destructively.** Always read → merge → write, preserving comments, atomic swap, pre-validated.
- **Don't auto-restart the daemon.** You decide when.
- **`config.toml` is source of truth.** If the TUI's sidecar view diverges, the config wins and the UI warns.

## Further reading

- **Per-feature docs:** [`docs/`](./docs/) — what each tab and setting does, when to touch it, keybindings reference.
- **Design notes:** [`CLAUDE.md`](./CLAUDE.md) — why the code is shaped the way it is.

## License

MIT
