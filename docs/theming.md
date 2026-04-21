# Theming

voxtype-tui auto-detects [Omarchy](https://omarchy.org)'s active theme and matches it, falls back to a built-in Textual theme otherwise, and supports a user override.

## Auto-detect

On startup the TUI reads `~/.config/omarchy/current/theme/alacritty.toml` and extracts four colors:

| TUI slot | From alacritty.toml |
|---|---|
| `primary` | `colors.normal.red` (fallback: `colors.bright.red`) |
| `accent` | `colors.normal.yellow` (fallback: `colors.bright.yellow`) |
| `foreground` | `colors.primary.foreground` |
| `background` | `colors.primary.background` |

A theme named `omarchy-auto` is registered and becomes the default if nothing else overrides it. Both `#RRGGBB` and `0xRRGGBB` color formats are accepted.

If the alacritty file is missing, unreadable, or malformed, the TUI silently falls back to the default Textual dark theme. No errors, no banner.

## User override

Drop your own theme at `~/.config/voxtype-tui/theme.toml` with four keys:

```toml
primary    = "#BF616A"
accent     = "#EBCB8B"
foreground = "#D8DEE9"
background = "#2E3440"
```

Any missing key falls through to a Nord-like default for that specific slot. The resulting theme is named `user` and takes precedence over `omarchy-auto` at startup.

## Picker

`ctrl+p` opens the Textual command palette. Scroll to the theme menu and pick any registered theme:

- Built-ins: `textual-dark`, `textual-light`, `nord`, `gruvbox`, etc.
- `omarchy-auto` if Omarchy is detected
- `user` if you've created the override file

Your selection is persisted to `~/.config/voxtype-tui/ui.json` — next launch starts with that theme.

## Live reload

Not supported in v1. If you change your Omarchy theme, the TUI needs to restart to pick up the new colors. Your selected theme *name* persists — you don't have to re-pick it.

## Why Omarchy is auto-detected but Gnome / KDE aren't

The TUI reads Omarchy's theme file directly because that file has a well-defined location and a stable schema. For other desktop environments, the `user` override path is the escape hatch — copy your terminal's four colors into `theme.toml` and you're done.
