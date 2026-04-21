# Dictionary

Post-transcription text replacements. After Whisper produces text, Voxtype runs each entry as a literal find-and-replace (case-insensitive) before the text reaches your cursor.

Stored as `[text] replacements` in the config. In the TUI, each entry also gets a **category tag** stored in the sidecar for your own organization — the category is a UI-only label, voxtype doesn't see it.

## What it actually does

```toml
[text]
replacements = { "cloud code" = "Claude Code", "slash deploy" = "/deploy" }
```

Matching is **case-insensitive** and **literal** — no regex. `"cloud code"` matches `"Cloud Code"`, `"CLOUD CODE"`, etc. It does not match inside larger words (e.g. `"cloudcode"`).

## The two UI categories

Both collapse to the same `[text] replacements` map on disk. The category is a tag *you* use to keep the list organized — voxtype itself just sees a flat dict and treats every entry as a literal rewrite. There's no behavioral difference between the two.

| Category | Typical use | Example |
|---|---|---|
| **Replacement** | Fix a recurring mis-hear, or trigger a slash-command by voice | `cloud code` → `Claude Code` / `slash deploy` → `/deploy` |
| **Capitalization** | Force a specific case | `type script` → `TypeScript` |

Press `c` on a selected row to cycle its category.

> Sidecar files created by older versions tagged slash-command entries with a third category, `Command`. Voxtype-tui now folds `Command` into `Replacement` (they were functionally identical — both are flat literal rewrites). Migration is silent and happens at load time; the disk file picks up the new tag on the next save.

## Keybindings

- `n` — focus the From input (or next match if search is active)
- `/` — focus the search input
- `d` — delete selected row
- `u` — undo last delete
- `c` — cycle the selected row's category
- `j` / `k` / `gg` / `G` — vim navigation

## Limits

- **Literal matches only** — no regex, no word boundaries, no conditional capitalization. If you need smarter logic, use a post-processing command instead ([see Settings → Post-processing](./settings.md#post-processing)).
- No **ordering** guarantees — voxtype applies all replacements; if two rules could fire on overlapping text, the result depends on voxtype's internals.

## When you clear the last entry

Deleting the only remaining replacement removes the entire `[text.replacements]` section from `config.toml`. The TUI shows a warning toast the first time this happens — any inline comments you had attached to entries inside that section are dropped along with it.

## Changes are restart-sensitive

`[text].replacements` is read into the daemon's text-layer cache at startup, not per-transcription. After saving, the TUI raises a RestartModal the first time (or leaves a persistent `⚠ Daemon restart needed` pill if you've already dismissed it this session); press `ctrl+shift+r` to restart the daemon once you're done editing.
