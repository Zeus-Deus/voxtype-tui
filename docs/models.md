# Models

List / download / delete / set-active for the model files that live in `~/.local/share/voxtype/models/`.

## Columns

| Column | Meaning |
|---|---|
| Model | Name Voxtype uses for this model (`base.en`, `large-v3-turbo`, …) |
| Size | On-disk size if downloaded; catalog estimate otherwise |
| Status | `downloaded` / `—` (not downloaded) / `unknown` (file on disk not in the catalog) |
| Active | `●` if this is the model voxtype is currently configured to use |

## The four actions

**Set active** — writes the engine's model path (`whisper.model` / `parakeet.model_type` / …) to match the selected row. Only flips the dirty indicator; you still press `ctrl+s` to persist. Because model-path fields are restart-sensitive, the save triggers the standard RestartModal.

**Download** — shells to `voxtype setup --download --model <name>`, streams the output into the log at the bottom, drives the progress bar from the `NN.N%` lines. Voxtype's download step **also rewrites `~/.config/voxtype/config.toml`** to set the just-downloaded model as active, so the TUI reloads state from disk after the download finishes. To avoid silently clobbering your unsaved edits, the Download button is disabled while the dirty indicator is on — save first (or reload), then download.

**Delete** — confirm modal (`y`/`n`/`esc`), then removes the `.bin` from the models dir. **Refuses to delete the currently active model** — set a different one active first. This is a guard against the easy mistake of deleting your way into a broken voxtype.

**Cancel** — only enabled during an in-flight download. Sends SIGTERM (waits up to 3s), then SIGKILL, then removes the partial `.bin` so the next download starts from scratch.

## Which model should I pick?

For whisper specifically:

| Model | Size | Latency | When to pick it |
|---|---|---|---|
| `tiny.en` | 75 MB | Very fast | Testing the pipeline end-to-end; dictating single-word commands |
| `base.en` | 140 MB | Fast | Default for English; good trade-off on modest hardware |
| `small.en` | 460 MB | Medium | Better accuracy on accents / noisy audio |
| `medium.en` | 1.5 GB | Slow on CPU | Serious accuracy; comfortable with a GPU |
| `large-v3` | 2.9 GB | Slowest | Best accuracy, multilingual |
| `large-v3-turbo` | 1.5 GB | Fast w/ GPU | Best quality-per-second for most GPU users |

Drop the `.en` for the multilingual variant. Rule of thumb: start with `base.en`, jump to `large-v3-turbo` if you have a GPU and care about accuracy.

## Unknown models

Any `.bin` in the models dir that doesn't match a catalog entry appears with `status = unknown`. Useful if you manually dropped a custom model — you can still delete it through the UI to manage disk usage.

## Vim navigation

`j` / `k` / `gg` / `G` work on the table; `r` refreshes the disk scan without reloading the whole app.

## Outside scope

The Models tab does **not** manage models pointed to by absolute paths in the Custom-path Engine field — those are user-owned files in locations the TUI doesn't scan. If you want to delete one, do it by hand.
