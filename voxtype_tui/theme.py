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

# Hyprland's looknfeel cascade — later files override earlier ones. Mirrors
# the order Hyprland itself applies sources, so the "last uncommented wins"
# rule gives us the same value Hyprland ends up using for window borders.
HYPRLAND_LOOKNFEEL_CASCADE: tuple[Path, ...] = (
    Path.home() / ".local" / "share" / "omarchy" / "default" / "hypr" / "looknfeel.conf",
    Path.home() / ".config" / "omarchy" / "current" / "theme" / "hyprland.conf",
    Path.home() / ".config" / "hypr" / "looknfeel.conf",
)

# Fallback when the cascade doesn't exist or parsing fails — matches
# Textual's normal non-modal look.
DEFAULT_MODAL_BORDER: str = "solid"

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


def load_omarchy_border_style(
    cascade: Iterable[Path] = HYPRLAND_LOOKNFEEL_CASCADE,
) -> str | None:
    """Read Hyprland's border_size / rounding from the looknfeel cascade and
    map it to a Textual border style. Returns None when the user isn't on
    Omarchy (the cascade's root file is part of the Omarchy default layout,
    so absence of *all* files signals a non-Omarchy environment).

    Mapping:
      rounding > 0           -> "round"
      border_size == 0       -> "blank"
      border_size >= 3       -> "heavy"
      otherwise (1 or 2)     -> "solid"

    `rounding` wins over `border_size` — if the user asked for rounded
    window corners in Hyprland, we prefer a rounded modal border even if
    the underlying thickness would otherwise map to heavy/solid. This
    matches Gazelle's behavior."""
    rounding: int = 0
    border_size: int = 2  # Omarchy's stock value
    saw_any_file = False

    for conf in cascade:
        if not conf.exists():
            continue
        saw_any_file = True
        try:
            for raw in conf.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().split("#", 1)[0].strip()  # strip trailing comments
                if key == "rounding":
                    try:
                        rounding = int(val)
                    except ValueError:
                        pass
                elif key == "border_size":
                    try:
                        border_size = int(val)
                    except ValueError:
                        pass
        except OSError:
            continue

    if not saw_any_file:
        return None

    if rounding > 0:
        return "round"
    if border_size == 0:
        return "blank"
    if border_size >= 3:
        return "heavy"
    return "solid"


def resolve_modal_border_style(
    cascade: Iterable[Path] = HYPRLAND_LOOKNFEEL_CASCADE,
) -> str:
    """Public entry point used by modal CSS — always returns a valid Textual
    border style. Falls back to DEFAULT_MODAL_BORDER when detection yields
    None (e.g., non-Omarchy system or unreadable cascade)."""
    detected = load_omarchy_border_style(cascade)
    return detected or DEFAULT_MODAL_BORDER


# Computed once at import time. CSS doesn't hot-reload, so the user restarts
# voxtype-tui to pick up Hyprland changes — same tradeoff Gazelle makes.
MODAL_BORDER_STYLE: str = resolve_modal_border_style()


def ensure_user_theme_template(path: Path = USER_THEME_TOML) -> bool:
    """Drop a starter theme.toml at `path` if nothing's there. Returns True
    if a file was created, False if one already existed."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(USER_THEME_TEMPLATE)
    return True
