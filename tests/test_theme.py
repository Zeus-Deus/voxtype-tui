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


# ---- load_omarchy_border_style ----


def _write_looknfeel(path: Path, *, border_size: int | None = None, rounding: int | None = None) -> None:
    """Write a minimal Hyprland looknfeel block with only the keys we care
    about. Hyprland accepts keys outside any `<section> { ... }` block, so
    this works without having to simulate the full nested syntax."""
    lines = [
        "# test fixture",
    ]
    if border_size is not None:
        lines.append(f"    border_size = {border_size}")
    if rounding is not None:
        lines.append(f"    rounding = {rounding}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def test_border_style_returns_none_when_no_cascade(tmp_path: Path) -> None:
    """A non-Omarchy system (no looknfeel files anywhere in the cascade)
    should return None so the caller can fall back."""
    missing = tmp_path / "nowhere.conf"
    assert theme_mod.load_omarchy_border_style([missing]) is None


def test_border_style_default_solid(tmp_path: Path) -> None:
    """Omarchy's stock config has border_size=2 and rounding=0 — that maps
    to a solid 1-cell Textual border."""
    conf = tmp_path / "looknfeel.conf"
    _write_looknfeel(conf, border_size=2, rounding=0)
    assert theme_mod.load_omarchy_border_style([conf]) == "solid"


def test_border_style_heavy_for_big_borders(tmp_path: Path) -> None:
    conf = tmp_path / "looknfeel.conf"
    _write_looknfeel(conf, border_size=4, rounding=0)
    assert theme_mod.load_omarchy_border_style([conf]) == "heavy"


def test_border_style_blank_for_zero(tmp_path: Path) -> None:
    conf = tmp_path / "looknfeel.conf"
    _write_looknfeel(conf, border_size=0, rounding=0)
    assert theme_mod.load_omarchy_border_style([conf]) == "blank"


def test_border_style_round_wins_over_size(tmp_path: Path) -> None:
    """If the user asked for rounded corners we honor that even when the
    raw thickness would otherwise map to heavy."""
    conf = tmp_path / "looknfeel.conf"
    _write_looknfeel(conf, border_size=4, rounding=8)
    assert theme_mod.load_omarchy_border_style([conf]) == "round"


def test_border_style_cascade_last_wins(tmp_path: Path) -> None:
    """Later files in the cascade override earlier ones — user override
    should beat Omarchy's default, same as Hyprland's own resolution."""
    base = tmp_path / "base.conf"
    override = tmp_path / "override.conf"
    _write_looknfeel(base, border_size=4, rounding=0)         # -> heavy
    _write_looknfeel(override, border_size=2, rounding=0)     # -> solid
    assert theme_mod.load_omarchy_border_style([base, override]) == "solid"


def test_border_style_ignores_commented_lines(tmp_path: Path) -> None:
    """Commented-out overrides must NOT win. In the user's actual looknfeel
    cascade, `# border_size = 0` is a hint, not an active setting — it
    should be ignored entirely so the earlier default survives."""
    base = tmp_path / "base.conf"
    override = tmp_path / "override.conf"
    _write_looknfeel(base, border_size=2, rounding=0)
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("# border_size = 8\n# rounding = 12\n")
    assert theme_mod.load_omarchy_border_style([base, override]) == "solid"


def test_resolve_modal_border_always_returns_string(tmp_path: Path) -> None:
    """The public entry point never returns None — it falls back to the
    documented default when detection fails."""
    missing = tmp_path / "absent.conf"
    assert theme_mod.resolve_modal_border_style([missing]) == theme_mod.DEFAULT_MODAL_BORDER
