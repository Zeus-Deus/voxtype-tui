# Keybindings reference

## App-level (work from anywhere)

| Key | Action |
|---|---|
| `1` | Switch to Vocabulary |
| `2` | Switch to Dictionary |
| `3` | Switch to Settings |
| `4` | Switch to Models |
| `ctrl+s` | Save — validates via voxtype, writes atomically, prompts for daemon restart if needed |
| `ctrl+r` | Reload config from disk (refuses if unsaved changes exist; save first) |
| `ctrl+shift+r` | Restart the voxtype daemon — clears the "daemon stale" pill. No-op when the daemon isn't actually stale. |
| `ctrl+p` | Command palette (includes the theme picker) |
| `ctrl+q` | Quit (confirms if unsaved changes exist) |

Digit keys automatically yield to Input widgets — typing `kubectl` in an Input inserts the characters, doesn't jump tabs.

## Vim table navigation

Works on any tab that shows a DataTable (Vocabulary, Dictionary, Models).

| Key | Action |
|---|---|
| `j` / `k` | Cursor down / up |
| `g g` | Jump to top (two quick presses within 500 ms) |
| `G` | Jump to bottom |
| `N` | Previous search match |

Letter keys auto-yield to Input widgets too.

## Per-tab actions

### Vocabulary

| Key | Action |
|---|---|
| `n` | Focus Add input (or next match if search is active) |
| `/` | Focus search input |
| `d` | Delete selected row (with undo toast) |
| `u` | Undo last delete |
| `escape` | Blur Input, return focus to the table |

### Dictionary

| Key | Action |
|---|---|
| `n` | Focus the From input (or next match if search is active) |
| `/` | Focus search input |
| `d` | Delete selected row |
| `u` | Undo last delete |
| `c` | Cycle the selected row's category (Replacement ↔ Capitalization) |
| `escape` | Blur Input, return focus to the table |

### Settings

No table, no per-tab shortcuts. Tab around with the usual Textual focus keys (`tab` / `shift+tab`). `escape` blurs the current Input.

### Models

| Key | Action |
|---|---|
| `d` | Delete the selected model's `.bin` file (with confirm modal) |
| `r` | Re-scan the models dir (pick up files added outside the TUI) |
| `/` | Focus the Engine Select |

## Mouse

Works everywhere. Click a table row to select. Click any button to activate. Click a Collapsible header in Settings to expand / collapse.

## Context-sensitive letters

The `n` key is context-sensitive on tabs with both a search input and an Add input:

- **Search input is empty** → `n` focuses the Add input (standard "new").
- **Search input has text** (filter active) → `n` cycles to the next matching row. `N` cycles backward.

This matches vim's `/pattern` → `n` / `N` navigation when a filter is the active mental context, and falls back to the "add new" verb when it isn't.
