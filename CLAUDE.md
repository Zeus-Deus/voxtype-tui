# voxtype-tui

A management frontend for **Voxtype** (Omarchy's bundled voice-to-text tool) that brings Vexis-style UX to Voxtype's otherwise-TOML-only configuration.

## The goal in one sentence

Give Voxtype the same ergonomic vocabulary + dictionary management that Vexis has, without forking Voxtype itself — just read/write `~/.config/voxtype/config.toml` behind a clean UI.

## Why this exists

The user (zeus) built **Vexis**, a Tauri-style desktop voice-to-text app for developers, living at `~/projects/vexis`. Vexis ships with a polished UI for:

1. **Vocabulary** — a list of proper nouns / jargon (e.g. `Codemux`, `Claude Code`, `shadcn`, `t3code`) injected into Whisper's initial prompt so the STT model is biased toward recognizing them.
2. **Dictionary** — three-stage post-processing applied *after* Whisper runs:
   - **Replacement** — simple text rewrites ("cloud code" → "Claude Code")
   - **Commands** — spoken slash-command triggers for agent CLIs like Claude Code ("slash codemux release" → "/codemux-release", which Claude Code then invokes as a skill)
   - **Capitalization** — case-fixes
3. **History** — log of past transcriptions (473 entries in screenshots)
4. **Settings** — standard model/audio/output controls
5. **Record** — push-to-talk capture button

The user wants to **try Voxtype** (Omarchy's bundled tool) to see if it's more reliable than Vexis, but isn't willing to give up Vexis's management UX. Voxtype has most of the same underlying capabilities, just exposed only through a TOML file.

## Approach: thin frontend, no fork

**Do not fork Voxtype.** Voxtype is a Rust daemon binary at `/usr/bin/voxtype`. Forking means maintaining a Rust codebase parallel to upstream — pure cost, zero benefit for this use case.

**Do** build a standalone app that reads/writes Voxtype's config file:
- Config lives at `~/.config/voxtype/config.toml`
- Default template: `/home/zeus/.local/share/omarchy/default/voxtype/config.toml`
- Voxtype picks up changes automatically (the daemon re-reads on new invocations; some changes may require `systemctl --user restart voxtype`)

Talk to the running daemon only when needed — via `voxtype status --format json`, `voxtype record toggle`, `voxtype setup model`, etc.

## Feature → Voxtype config key map

This is the contract. Everything in the Vexis UI has a corresponding Voxtype config key (or can be synthesized).

### Vocabulary tab → `[whisper] initial_prompt`

Voxtype exposes a single free-text string:
```toml
[whisper]
initial_prompt = "Codemux, Claude Code, Vexis, Omarchy, waybar, Arch Linux, omarchy-kb, Hyprland, MCP, Superset, shadcn, OAuth, Claude, t3code, Bitwarden, Voxtype"
```
Our UI manages it as a **list of words/phrases**; on save we serialize to a comma-separated string. On load we parse it back into a list. Keep user-facing metadata (per-word notes, dates added, categories) in our own sidecar file — see "Sidecar" section below.

Note: Whisper's initial prompt is capped at ~224 tokens. The UI should show a running token count and warn when the list is close to the limit.

### Dictionary → Replacement / Commands / Capitalization → `[text] replacements`

Voxtype has exactly one replacement map:
```toml
[text]
replacements = { "vox type" = "voxtype", "cloud code" = "Claude Code", "slash codemux release" = "/codemux-release" }
```
Case-insensitive by default. All three Vexis categories collapse into this map — **we preserve the category as our own metadata** (sidecar file) and apply it as a UI-only tag. On disk, Voxtype just sees flat key→value pairs.

If the user wants smarter logic than literal replacements (e.g. contextual capitalization, regex), we can optionally generate a `post_process_command` script. Keep this simple for v1: literal replacements only.

### Settings tab → direct config keys

| UI control | Config key |
|---|---|
| Engine | top-level `engine` (lowercase: `whisper` / `parakeet` / `moonshine` / …). Voxtype's `voxtype config` output groups it under a fake `[engine]` header but the storage form is a top-level key. Required sections per empirical validation: `[hotkey]` (needs `key`), `[audio]` (needs `device`, `sample_rate`, `max_duration_secs`), `[whisper]`, `[output]` (needs `mode`). |
| Model | `[whisper] model` or `[parakeet] model_type` etc. |
| Language | `[whisper] language` |
| Hotkey | `[hotkey] key` + `[hotkey] modifiers` |
| Activation mode | `[hotkey] mode` (push_to_talk / toggle) |
| Audio device | `[audio] device` |
| Max recording duration | `[audio] max_duration_secs` |
| Output mode | `[output] mode` (type / clipboard / paste) |
| Auto-submit | `[output] auto_submit` |
| Spoken punctuation | `[text] spoken_punctuation` |
| Smart auto-submit ("say submit") | `[text] smart_auto_submit` |
| GPU acceleration | run `voxtype setup gpu` as a subcommand |
| Post-process LLM hook | `[output.post_process] command` + `timeout_ms` |
| Remote backend | `[whisper] remote_endpoint`, `remote_model`, `remote_api_key` |

Full key list: see `voxtype config` output and the `strings` dump notes below.

### History tab → **not provided by Voxtype**

Voxtype doesn't persist transcription history. To replicate Vexis's History tab we'd need to log transcriptions ourselves. Two options:

1. **Defer** — ship v1 without History; tell the user to use Vexis's History if they need it.
2. **Tail** — wrap `voxtype status --follow --format json` and capture transcriptions as they happen, store to our own SQLite DB.

Recommend option 1 for v1. Add option 2 later only if the user misses it.

### Record tab → `voxtype record toggle`

Voxtype already binds Super+Ctrl+X globally in Hyprland, so an in-app Record button is arguably redundant. If we want one for parity with Vexis, it just shells out to `voxtype record toggle`.

## Sidecar metadata file

We need to store UI-only metadata that doesn't belong in Voxtype's config:
- Per-vocabulary-word category / notes / timestamps
- Per-replacement-rule category tag (Replacement vs Commands vs Capitalization)

Put this at `~/.config/voxtype-tui/metadata.json` (or `.toml`). On load, merge with `~/.config/voxtype/config.toml`. On save, write both files.

**Important:** if the user edits `~/.config/voxtype/config.toml` by hand (e.g. through the Omarchy waybar right-click integration, which opens the file in their editor), our sidecar can drift. On load, reconcile: anything in Voxtype's config that's not in our sidecar gets a default category.

## Tech stack — decided: Python + Textual

**Decision:** Python TUI using [Textual](https://textual.textualize.io/). Rationale: this app only reads/writes a TOML file and shells out to `voxtype` — no performance-critical work, so Rust/Ratatui's advantages don't apply here. Textual gets us to v1 faster.

### Distribution

**Shipped installs go through AUR (`yay -S voxtype-tui`) or pipx** (`pipx install voxtype-tui`). The PKGBUILD uses the modern `python-build` + `python-installer` flow; `.SRCINFO` lives at the repo root and must be regenerated with `makepkg --printsrcinfo > .SRCINFO` whenever PKGBUILD changes. `tests/test_pkgbuild.py` fails loudly when the committed `.SRCINFO` diverges from what PKGBUILD produces.

`scripts/install-omarchy.sh` assumes `voxtype-tui` is already on `$PATH` from a system-level install (AUR / pipx). It does **not** drop a conda wrapper — if the binary isn't found it exits with a message pointing at the package managers.

### Development environment

**Conda is for local dev only**, not a supported user install path. Zeus's machine has **miniconda** at `/home/zeus/miniconda3` with a dedicated env named **`voxtype-tui`**.

```bash
# Activate before doing anything in this project
source /home/zeus/miniconda3/etc/profile.d/conda.sh
conda activate voxtype-tui

# Python 3.12, with: textual, tomlkit, pytest (+ transitive deps)
# Dependencies pinned in ./requirements.txt; also declared in pyproject.toml.
# Recreate from scratch if needed:
#   conda create -n voxtype-tui python=3.12 pip -y
#   conda activate voxtype-tui
#   pip install -e ".[dev]"
```

### Key libraries

- **textual** — TUI framework, polished widgets, hot-reload via `textual run --dev app.py`
- **tomlkit** — TOML library that **preserves comments and formatting** on round-trip. Critical for `~/.config/voxtype/config.toml` since the default file has useful inline comments we shouldn't destroy. Do NOT use the stdlib `tomllib` (read-only) or `toml` (loses comments).
- **stdlib**: `subprocess` for shelling out to `voxtype`, `json` for the sidecar, `pathlib` for paths.

## Non-goals

- **Do not fork Voxtype.** Use it as-is.
- **Do not reimplement Whisper / STT.** That's Voxtype's job.
- **Do not auto-restart the Voxtype daemon** on every tiny config change — batch changes and only restart when a field that requires it is touched (hotkey, engine, model).
- **Do not write to `~/.config/voxtype/config.toml` destructively.** Always read → merge → write. Preserve user comments if possible (use a comment-preserving TOML library).

## Useful commands & file locations

```bash
# Voxtype
which voxtype                         # /usr/bin/voxtype
voxtype config                        # pretty-print current resolved config
voxtype status --format json          # JSON state (idle / recording / transcribing)
voxtype status --follow --format json # stream state changes
voxtype setup model                   # interactive model picker
voxtype setup gpu --enable            # enable Vulkan/CUDA/ROCm
voxtype record toggle                 # start/stop recording
systemctl --user status voxtype       # daemon status
systemctl --user restart voxtype      # reload config

# Files
~/.config/voxtype/config.toml                           # main config
~/.local/share/voxtype/models/                          # downloaded models
/run/user/1000/voxtype/state                            # state file (JSON)
/home/zeus/.local/share/omarchy/default/voxtype/        # Omarchy's default template
/home/zeus/.local/share/omarchy/bin/omarchy-voxtype-*   # Omarchy helper scripts
```

## User context

- **User:** zeus (widow.cc) — builds desktop apps (see Vexis), runs Omarchy on Arch+Hyprland, has an RTX 3090
- **Vexis source:** `~/projects/vexis` — reference for UX patterns, not to be copied literally
- **Primary use case:** dictating slash-commands into Claude Code and other agent CLIs, with proper-noun recognition for project names

## When resuming this project

1. Read this file.
2. Check `~/.config/voxtype/config.toml` for the current user config — it may have been hand-edited.
3. Check `~/projects/voxtype-tui/` for whatever scaffolding exists.
4. Ask the user what stack was picked if it's not recorded here yet.
