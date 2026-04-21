"""Theme loading with Omarchy auto-detect.

Auto-detects Omarchy's active theme via `~/.config/omarchy/current/theme/
alacritty.toml` and registers a matching Textual Theme. If the user drops a
`~/.config/voxtype-tui/theme.toml` with their own colors, that takes
precedence. Selection is exposed through Textual's built-in command palette
(ctrl+p) and persisted to `~/.config/voxtype-tui/ui.json`.

Design decisions:
  * Preference (`ui.json`) and user color override (`theme.toml`) live as
    separate files so the "what theme is selected" is distinct from "how
    does my custom theme look".
  * No live reload — Omarchy theme changes require an app restart. If the
    user explicitly changes voxtype-tui's theme via the palette, it sticks
    immediately and survives restart via the ui.json watcher.
  * Fallback chain: user > omarchy-auto > textual-dark.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

try:
    import tomllib
except ImportError:  # pragma: no cover — Python <3.11 fallback
    tomllib = None  # type: ignore[assignment]

from textual.theme import Theme

OMARCHY_THEME_FILE = Path.home() / ".config" / "omarchy" / "current" / "theme" / "alacritty.toml"
CONFIG_DIR = Path.home() / ".config" / "voxtype-tui"
UI_JSON = CONFIG_DIR / "ui.json"
USER_THEME_TOML = CONFIG_DIR / "theme.toml"

# Nord-flavored defaults, used when a color is missing from the source TOML.
DEFAULT_COLORS: dict[str, str] = {
    "primary": "#BF616A",
    "accent": "#EBCB8B",
    "foreground": "#D8DEE9",
    "background": "#2E3440",
}

USER_THEME_TEMPLATE = """# voxtype-tui user theme override
#
# All four keys accept #RRGGBB or 0xRRGGBB. Restart voxtype-tui after edits;
# the resulting "user" theme then shows up in the command palette (ctrl+p).

primary    = "#BF616A"
accent     = "#EBCB8B"
foreground = "#D8DEE9"
background = "#2E3440"
"""


def normalize_color(color: object) -> str | None:
    """Convert `0xRRGGBB` to `#RRGGBB`. Returns None for anything that
    doesn't look like a usable color string."""
    if not isinstance(color, str) or not color:
        return None
    if color.startswith("0x") and len(color) == 8:
        return "#" + color[2:]
    if color.startswith("#") and len(color) in (4, 7):
        return color
    return None


def load_omarchy_colors(path: Path = OMARCHY_THEME_FILE) -> dict[str, str] | None:
    """Parse Omarchy's alacritty theme into our 4-color palette. Returns None
    if the file doesn't exist, tomllib is unavailable, or parsing fails."""
    if tomllib is None:
        return None
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text())
    except Exception:
        return None
    colors = data.get("colors") if isinstance(data, dict) else None
    if not isinstance(colors, dict):
        return None
    primary_section = colors.get("primary") if isinstance(colors, dict) else None
    normal = colors.get("normal") if isinstance(colors, dict) else None
    bright = colors.get("bright") if isinstance(colors, dict) else None
    primary_section = primary_section if isinstance(primary_section, dict) else {}
    normal = normal if isinstance(normal, dict) else {}
    bright = bright if isinstance(bright, dict) else {}

    return {
        "accent": (
            normalize_color(normal.get("yellow"))
            or normalize_color(bright.get("yellow"))
            or DEFAULT_COLORS["accent"]
        ),
        "primary": (
            normalize_color(normal.get("red"))
            or normalize_color(bright.get("red"))
            or DEFAULT_COLORS["primary"]
        ),
        "foreground": (
            normalize_color(primary_section.get("foreground"))
            or DEFAULT_COLORS["foreground"]
        ),
        "background": (
            normalize_color(primary_section.get("background"))
            or DEFAULT_COLORS["background"]
        ),
    }


def load_user_colors(path: Path = USER_THEME_TOML) -> dict[str, str] | None:
    """Parse the user's `theme.toml` — a flat file with primary/accent/
    foreground/background keys. Returns None if absent or unparseable."""
    if tomllib is None:
        return None
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "primary": normalize_color(data.get("primary")) or DEFAULT_COLORS["primary"],
        "accent": normalize_color(data.get("accent")) or DEFAULT_COLORS["accent"],
        "foreground": normalize_color(data.get("foreground")) or DEFAULT_COLORS["foreground"],
        "background": normalize_color(data.get("background")) or DEFAULT_COLORS["background"],
    }


def build_theme(name: str, colors: dict[str, str]) -> Theme:
    return Theme(
        name=name,
        primary=colors["primary"],
        secondary=colors["accent"],
        accent=colors["accent"],
        foreground=colors["foreground"],
        background=colors["background"],
        surface=colors["background"],
        panel=colors["background"],
        dark=True,
    )


def load_ui_prefs(path: Path = UI_JSON) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_ui_prefs(prefs: dict, path: Path = UI_JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=2) + "\n")


def ensure_user_theme_template(path: Path = USER_THEME_TOML) -> bool:
    """Drop a starter theme.toml at `path` if nothing's there. Returns True
    if a file was created, False if one already existed."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(USER_THEME_TEMPLATE)
    return True
