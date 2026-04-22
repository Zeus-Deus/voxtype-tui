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

### Dictionary → Replacement / Commands / Capitalization → `[text] replacements` + `[output.post_process]`

Two layers run against every transcript:

1. **Voxtype native** — `[text] replacements = { "vox type" = "voxtype", …}` is a flat case-insensitive `\b`-bounded map applied inside the Voxtype daemon (`src/text/mod.rs`). Fast, but limited to literal triggers with ASCII word boundaries.

2. **voxtype-tui post-processor (Vexis-parity)** — when the sidecar has any rules, saves wire `[output.post_process] command = "voxtype-tui-postprocess"`. The CLI (`voxtype_tui.cli_postprocess`) re-reads the sidecar on every transcription and runs a two-stage engine (`voxtype_tui.dictionary_engine`):
   - **Stage 1 (Fuzzy replacements)** — Whisper-aware bounded-gap regex. One trigger `"slash codemux release"` matches `slash codemux release`, `/codemux release`, `/codemuxrelease`, `/codemux-release`, `/codemuxapi-release`, etc. Command-phrase atoms (`slash` → `(?:slash|/)` etc.) match both spoken and auto-formatted forms.
   - **Stage 2 (Exact capitalization)** — strict whole-token match with Unicode neighbor-char boundaries (handles `c++`, `.net`). Runs after replacements so proper-noun casing gets fixed post-hoc.

Sidecar holds category + `to_text`. Config.toml `[text] replacements` mirrors Replacement-category rules as a native-layer fallback (if `voxtype-tui-postprocess` is missing, Voxtype's own literal pass still catches them).

Vexis's Stage 2 (built-in spoken-command expansion) is intentionally skipped — Voxtype's `spoken_punctuation` already handles `slash → /`, `hash → #`, etc.

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
- Per-replacement-rule category tag (Replacement vs Capitalization) — UI-organizational only; voxtype sees a flat map

Put this at `~/.config/voxtype-tui/metadata.json` (or `.toml`). On load, merge with `~/.config/voxtype/config.toml`. On save, write both files.

**Important:** if the user edits `~/.config/voxtype/config.toml` by hand (e.g. through the Omarchy waybar right-click integration, which opens the file in their editor), our sidecar can drift. On load, reconcile: anything in Voxtype's config that's not in our sidecar gets a default category.

## Tech stack — decided: Python + Textual

**Decision:** Python TUI using [Textual](https://textual.textualize.io/). Rationale: this app only reads/writes a TOML file and shells out to `voxtype` — no performance-critical work, so Rust/Ratatui's advantages don't apply here. Textual gets us to v1 faster.

### Distribution

**Shipped installs go through AUR** (`yay -S voxtype-tui`). voxtype-tui is Arch-only at the moment; it is not published to PyPI, so pipx is not an install path. The PKGBUILD uses the modern `python-build` + `python-installer` flow; `.SRCINFO` lives at the repo root and must be regenerated with `makepkg --printsrcinfo > .SRCINFO` whenever PKGBUILD changes. `tests/test_pkgbuild.py` fails loudly when the committed `.SRCINFO` diverges from what PKGBUILD produces.

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

## v1.1: portable bundle / sync / export / import

After v1 ships, the next feature is a Syncthing-friendly JSON bundle that
also doubles as the manual export/import format and the Vexis migration
path. One file, three uses: **auto-sync**, **manual backup**, **migrate
to a new machine**. None of them required — a user who never touches
Syncthing still gets a safe, hand-editable backup format for free.

Module: `voxtype_tui/sync.py` (pure functions; no I/O at this layer).
See the module docstring for the in-code contract; everything below is
the *why*.

### Principles

- **Sync is optional.** The app must start and run identically whether
  `~/.config/voxtype-tui/sync.json` exists or not. Reads from
  `config.toml` + `metadata.json` remain the source of truth. The bundle
  is a *derived artifact* plus an import channel — never load-bearing.
  If the user deletes `sync.json`, it regenerates on next save. If it's
  unwritable, we surface a banner and keep running.
- **Content-hash staleness compare, not mtime.** `sync.json` carries a
  `local_sync_hash` — the sha256 of the distilled `sync` block at the
  moment we wrote the file. On startup the reader recomputes the hash
  from live state and compares. Equal → local hasn't changed → apply the
  synced bundle. Different → local drifted → rewrite sync.json. No clock
  trust; survives `touch`, backups, editor mtime-preserving writes.
- **Dangerous paths are explicit.** Any change to an API key or any
  `*_command` shell string is treated as an explicit user decision on
  import, never a silent apply. Preview modal shows the exact string
  before the user confirms. See `sync.DANGEROUS_PATHS`.

### Block layout

Bundles are JSON with three distinct blocks. The presence or absence of
each is the security signal:

| Block | Auto-sync write | Manual export | Purpose |
|---|---|---|---|
| `sync` | ✅ always | ✅ always | Vocabulary, replacements, portable settings. Safe to sync / share. |
| `local` | ✅ always | optional scope | Hotkey, audio, VAD — per-device. Never auto-applied on sync-import; only applied when the user explicitly imports "local settings too". |
| `secrets` | ❌ **never** | only with **Include secrets** toggle (default OFF) | API keys and `*_command` shell strings. Absence = file is safe to hand around. |

The `secrets` block is separate on purpose: a glance at the top-level
keys tells you whether the file contains sensitive material. One opt-in
per manual export; no other knob.

### Field policy

See `sync.SECRET_PATHS` and `sync.DANGEROUS_PATHS` for the authoritative
list. At a glance:

- **Stripped from sync, only in `secrets` with toggle**: `whisper.remote_api_key`, `output.post_process.command`, `output.pre_output_command`, `output.post_output_command`. Each is either a credential or an RCE surface.
- **Always-sync, but flagged on import**: `whisper.remote_endpoint`. Not a credential, but a malicious change exfils audio to an attacker's server — so any *change* on import must be confirmed.
- **Always-sync, no special handling**: everything else portable (engine, model, language, output mode, text toggles, per-engine model keys).
- **Always local (never sync-applied)**: hotkey (keyboard-dependent), audio.device (hardware-dependent), audio.feedback, audio.sample_rate, audio.max_duration_secs, VAD, state_file. The bundle carries them for manual export-all; the sync reader ignores the `local` block.

### Hard caps (protect the importer)

- Bundle size: 1 MB (`MAX_BUNDLE_BYTES`)
- Vocab: 500 entries, 200 chars per phrase
- Replacements: 1000 entries, 500 chars per side
- Any single string setting: 2048 chars
- Device label: 128 chars
- Whisper initial_prompt token estimate warned at 224 tokens (~4 chars/token heuristic; `estimate_initial_prompt_tokens`)

Schema version: integer, currently 1. Reader refuses to parse a newer
version — graceful banner rather than a partial apply.

### Vexis import

The adapter supports Vexis's two separate export files, detected by
shape (`detect_format`):

- `vexis-dictionary.json` → list of `{id, trigger, replacement, category}` with `category ∈ {replacement, command, capitalization}`. We map `trigger→from`, `replacement→to`, categorize case-sensitively (`replacement`/`command` → `Replacement`; `capitalization` → `Capitalization`). The `command` → `Replacement` migration matches Vexis's own storage migration (the `Command` variant is a legacy ghost; see `vexis/src-tauri/src/dictionary/mod.rs::EntryCategory::Command`).
- `vexis-vocabulary.json` → either a plain string list or a list of `{id, word}`. We dedupe case-insensitively to match Vexis's `UNIQUE COLLATE NOCASE` constraint.

Our own format is detected via the `format: "voxtype-tui-bundle"` tag.

### File location

- `~/.config/voxtype-tui/sync.json` — the sync file. Atomic-rename writes, idempotent: identical sync-block hash → skip the rename (avoids Syncthing thrash on unrelated local edits).
- Manual exports default to `~/Downloads/voxtype-tui-export-YYYY-MM-DD.json` (editable path in the export dialog).

### Shipping order (after v1)

1. **`sync.py` + tests** — schema, distill, hash, strip, adapters. Pure; no I/O. ✅ *done* (`voxtype_tui/sync.py`, `tests/test_sync.py`).
2. **Writer on save** — atomic tempfile + rename, idempotent via hash, wired into `AppState.save`.
3. **Manual export screen** — Ctrl+E. Options: scope (Sync only / Sync + Local), Include-secrets toggle (default OFF), target path.
4. **Manual import + Vexis adapter** — Ctrl+I. File picker → `detect_format` → preview modal showing diff, including the full text of any dangerous-field changes → confirm.
5. **Startup reader** — content-hash compare, apply sync if local unchanged, banner "Synced from {device}".
6. **Conflict detection** — scan `sync.sync-conflict-*.json`, banner, manual-resolve screen.
7. **Model-missing banner** — wire to the existing Models-tab download pipeline when a synced `whisper.model` / `parakeet.model_type` / etc. isn't on disk.

Commit between each step. No step touches the daemon without explicit
user action.

## Engine availability + Models tab

Voxtype supports 7 engines (whisper, parakeet, moonshine, sensevoice, paraformer, dolphin, omnilingual) but most are gated behind Cargo features. The upstream AUR `voxtype-bin` package ships **whisper-only**; the others need a custom `cargo build --features moonshine,parakeet,…` to unlock.

voxtype-tui detects compiled engines at Models-tab mount via `voxtype_cli.compiled_engines()`, which runs `voxtype setup model` with stdin closed and scans for `(not available - rebuild with --features X)` markers. The detected set drives two pieces of UI:

- Uncompiled engines show `<name> (not compiled)` in the engine Select dropdown — users see why Download is unavailable.
- The Download button is disabled when the selected engine is uncompiled. It would otherwise fire a subprocess guaranteed to fail with a rebuild-instruction error.

Whisper + every compiled non-whisper engine goes through the same in-app subprocess flow (`voxtype setup --download --model <NAME>`), same progress bar, same refresh semantics. On probe failure (missing binary, timeout, unparseable output) the fallback is whisper-only so basic dictation stays functional. See `voxtype_cli.py` for the parser and `models.py` for the wiring.

## Schema migrations + update propagation

New releases can ship with schema changes (e.g. "enable the post-process CLI for any existing install that has rules"). Three triggers all funnel into one pure function — `migrations.run_pending(doc, sc)` — so a new feature only needs ONE place to wire an auto-activation:

1. **TUI startup** — `AppState.load()` runs pending migrations after reconcile, sets `config_dirty`/`daemon_stale` so the next save + exit picks them up.
2. **Headless CLI** — `voxtype-tui --apply-migrations` (implemented in `cli_migrate.py`, dispatched from `app.main()` before Textual boots). Loads state, runs migrations, saves, restarts the daemon. Idempotent. Intended for power users and the AUR post-install hook.
3. **AUR pacman hook** — `voxtype-tui.install` (at repo root, referenced via `install=voxtype-tui.install` in PKGBUILD). Shown to users on install/upgrade; tells them migrations will apply on next TUI launch and offers the CLI one-liner for immediate activation. The hook does NOT invoke anything itself (it runs as root and can't touch per-user config + user systemd service reliably).

**Schema version**: `sidecar.SCHEMA_VERSION` (int, currently `2`). Fresh in-memory sidecars start at SCHEMA_VERSION so they skip pending migrations; file-loaded sidecars use whatever version the file declares (default `1` for pre-migration-era sidecars). Migrations advance `sc.version` even when no individual migration had visible work — the version is the claim "every migration up to N has been considered".

**Adding a migration**: append to `migrations.MIGRATIONS` with `target_version = SCHEMA_VERSION + 1`, bump `SCHEMA_VERSION`, add a test. The migration function is idempotent by contract and takes `(doc, sc)` → returns `True` when it changed anything.

## Non-goals

- **Do not fork Voxtype.** Use it as-is.
- **Do not reimplement Whisper / STT.** That's Voxtype's job.
- **Do not auto-restart the Voxtype daemon mid-session.** Voxtype reads its config into memory at daemon start (`src/daemon.rs` → `Daemon::new`, `src/text/mod.rs:27-40`), so ANY `~/.config/voxtype/config.toml` write needs a restart — not just the narrow `RESTART_SENSITIVE_PATHS` list. But rapid-fire restarts during a settings session would interrupt the hotkey listener and thrash audio capture. So: in-session we only set the `daemon_stale` pill (manual `Ctrl+Shift+R` still available); on TUI exit, `_restart_daemon_on_exit_if_needed` silently restarts via `systemctl --user restart voxtype` with a one-line terminal notice. Sidecar-only edits (category flips, notes) never trigger — the post-process CLI re-reads metadata.json on every transcription.
- **Do not write to `~/.config/voxtype/config.toml` destructively.** Always read → merge → write. Preserve user comments if possible (use a comment-preserving TOML library).
- **Do not auto-sync secrets.** `whisper.remote_api_key` and all `*_command` fields are stripped from every auto-sync write and only appear in manually-exported bundles when the user explicitly toggles Include-secrets.
- **Do not trust wall-clock timestamps** for staleness compares. `local_sync_hash` is authoritative; `generated_at` is display-only.
- **Do not auto-download missing models on sync-import.** Surface the missing model via banner with an explicit download button.

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
~/.config/voxtype-tui/metadata.json                     # sidecar (UI-only metadata)
~/.config/voxtype-tui/sync.json                         # v1.1 portable bundle (optional, auto-generated)
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
