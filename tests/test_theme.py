"""Theme loading: Omarchy detection, user overrides, color normalization,
and graceful fallback when anything's absent or malformed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from voxtype_tui import theme as theme_mod


# ---- normalize_color ----

def test_normalize_0x_prefix() -> None:
    assert theme_mod.normalize_color("0x1a2b3c") == "#1a2b3c"


def test_normalize_hash_prefix() -> None:
    assert theme_mod.normalize_color("#1a2b3c") == "#1a2b3c"


def test_normalize_short_hash() -> None:
    assert theme_mod.normalize_color("#abc") == "#abc"


def test_normalize_empty_returns_none() -> None:
    assert theme_mod.normalize_color("") is None


def test_normalize_none_returns_none() -> None:
    assert theme_mod.normalize_color(None) is None


def test_normalize_garbage_returns_none() -> None:
    assert theme_mod.normalize_color("not a color") is None


# ---- load_omarchy_colors ----

def test_load_omarchy_colors_missing_file(tmp_path: Path) -> None:
    assert theme_mod.load_omarchy_colors(tmp_path / "nope.toml") is None


def test_load_omarchy_colors_valid_toml(tmp_path: Path) -> None:
    p = tmp_path / "alacritty.toml"
    p.write_text(
        '[colors.primary]\n'
        'background = "#1e1e2e"\n'
        'foreground = "#cdd6f4"\n'
        '[colors.normal]\n'
        'red = "#f38ba8"\n'
        'yellow = "#f9e2af"\n'
        '[colors.bright]\n'
        'yellow = "#f9e2af"\n'
    )
    colors = theme_mod.load_omarchy_colors(p)
    assert colors == {
        "background": "#1e1e2e",
        "foreground": "#cdd6f4",
        "primary": "#f38ba8",
        "accent": "#f9e2af",
    }


def test_load_omarchy_colors_0x_format(tmp_path: Path) -> None:
    """Some Omarchy themes use the `0xRRGGBB` form — should normalize."""
    p = tmp_path / "alacritty.toml"
    p.write_text(
        '[colors.primary]\n'
        'background = "0x1e1e2e"\n'
        'foreground = "0xcdd6f4"\n'
        '[colors.normal]\n'
        'red = "0xf38ba8"\n'
        'yellow = "0xf9e2af"\n'
    )
    colors = theme_mod.load_omarchy_colors(p)
    assert colors is not None
    assert colors["background"] == "#1e1e2e"
    assert colors["primary"] == "#f38ba8"


def test_load_omarchy_colors_missing_sections_fills_defaults(tmp_path: Path) -> None:
    """A partial theme file should yield a palette that's still usable,
    falling back to DEFAULT_COLORS for anything missing."""
    p = tmp_path / "alacritty.toml"
    p.write_text('[colors.primary]\nbackground = "#111111"\n')
    colors = theme_mod.load_omarchy_colors(p)
    assert colors is not None
    assert colors["background"] == "#111111"
    assert colors["primary"] == theme_mod.DEFAULT_COLORS["primary"]
    assert colors["accent"] == theme_mod.DEFAULT_COLORS["accent"]
    assert colors["foreground"] == theme_mod.DEFAULT_COLORS["foreground"]


def test_load_omarchy_colors_truncated_toml(tmp_path: Path) -> None:
    p = tmp_path / "alacritty.toml"
    p.write_text('[colors.primary]\nbackground = "#111111"\n[colors.normal\n')
    assert theme_mod.load_omarchy_colors(p) is None


def test_load_omarchy_colors_non_toml(tmp_path: Path) -> None:
    p = tmp_path / "alacritty.toml"
    p.write_text('this is definitely not toml = oops')
    assert theme_mod.load_omarchy_colors(p) is None


def test_load_omarchy_colors_bright_fallback(tmp_path: Path) -> None:
    """If `normal.yellow` is missing but `bright.yellow` is present, the
    accent should come from bright."""
    p = tmp_path / "alacritty.toml"
    p.write_text(
        '[colors.primary]\nbackground = "#222"\nforeground = "#eee"\n'
        '[colors.bright]\nyellow = "#abcdef"\nred = "#fedcba"\n'
    )
    colors = theme_mod.load_omarchy_colors(p)
    assert colors is not None
    assert colors["accent"] == "#abcdef"
    assert colors["primary"] == "#fedcba"


# ---- load_user_colors ----

def test_load_user_colors_valid(tmp_path: Path) -> None:
    p = tmp_path / "theme.toml"
    p.write_text(
        'primary    = "#aa0000"\n'
        'accent     = "#00aa00"\n'
        'foreground = "#cccccc"\n'
        'background = "#000000"\n'
    )
    colors = theme_mod.load_user_colors(p)
    assert colors == {
        "primary": "#aa0000",
        "accent": "#00aa00",
        "foreground": "#cccccc",
        "background": "#000000",
    }


def test_load_user_colors_missing_returns_none(tmp_path: Path) -> None:
    assert theme_mod.load_user_colors(tmp_path / "nope.toml") is None


def test_load_user_colors_partial_uses_defaults(tmp_path: Path) -> None:
    p = tmp_path / "theme.toml"
    p.write_text('primary = "#ff0000"\n')
    colors = theme_mod.load_user_colors(p)
    assert colors is not None
    assert colors["primary"] == "#ff0000"
    assert colors["accent"] == theme_mod.DEFAULT_COLORS["accent"]


# ---- build_theme ----

def test_build_theme_has_all_required_slots() -> None:
    colors = {
        "primary": "#111111",
        "accent": "#222222",
        "foreground": "#333333",
        "background": "#444444",
    }
    theme = theme_mod.build_theme("test", colors)
    assert theme.name == "test"
    assert theme.primary == "#111111"
    assert theme.accent == "#222222"
    assert theme.foreground == "#333333"
    assert theme.background == "#444444"
    assert theme.surface == "#444444"
    assert theme.panel == "#444444"
    assert theme.dark is True


# ---- ui.json round-trip ----

def test_ui_prefs_empty_when_missing(tmp_path: Path) -> None:
    assert theme_mod.load_ui_prefs(tmp_path / "ui.json") == {}


def test_ui_prefs_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "ui.json"
    theme_mod.save_ui_prefs({"theme": "omarchy-auto"}, p)
    assert theme_mod.load_ui_prefs(p) == {"theme": "omarchy-auto"}


def test_ui_prefs_corrupt_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "ui.json"
    p.write_text("{not json")
    assert theme_mod.load_ui_prefs(p) == {}


# ---- template generation ----

def test_ensure_user_theme_template_creates_once(tmp_path: Path) -> None:
    p = tmp_path / "voxtype-tui" / "theme.toml"
    assert theme_mod.ensure_user_theme_template(p) is True
    assert p.exists()
    # Second call should be a no-op
    original = p.read_text()
    assert theme_mod.ensure_user_theme_template(p) is False
    assert p.read_text() == original
