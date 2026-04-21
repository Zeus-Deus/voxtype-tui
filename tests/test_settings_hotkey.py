"""Hotkey & Activation section of the Settings pane."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from textual.widgets import Checkbox, Input, Select, Switch

from voxtype_tui import config, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.settings import HOTKEY_MODIFIERS, HOTKEY_MODES, SettingsPane
from .conftest import FIXTURES


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)

    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)
    return cfg, side


async def _goto_settings(pilot, app):
    await pilot.press("3")
    await pilot.pause()
    assert app.query_one("#tabs").active == "settings"


async def test_hotkey_hydrates_from_stock(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        assert pane.query_one("#settings-hotkey-key", Input).value == "SCROLLLOCK"
        for mod in HOTKEY_MODIFIERS:
            cb = pane.query_one(f"#settings-mod-{mod.lower()}", Checkbox)
            assert cb.value is False
        assert pane.query_one("#settings-hotkey-mode", Select).value == "push_to_talk"
        assert pane.query_one("#settings-hotkey-enabled", Switch).value is True
        assert app.state.dirty is False


async def test_hotkey_hydrates_from_heavily_customized(tmp_path, monkeypatch):
    """heavily_customized.toml has a RIGHTALT + LEFTCTRL modifier + toggle
    mode — verifies the modifier checkbox group and mode Select populate
    correctly from a non-default config."""
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "heavily_customized.toml", cfg)

    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        assert pane.query_one("#settings-hotkey-key", Input).value == "RIGHTALT"
        assert pane.query_one("#settings-mod-leftctrl", Checkbox).value is True
        assert pane.query_one("#settings-mod-leftalt", Checkbox).value is False
        assert pane.query_one("#settings-hotkey-mode", Select).value == "toggle"
        assert pane.query_one("#settings-hotkey-enabled", Switch).value is True
        # config_dirty must stay False on mount — sidecar_dirty may be True
        # here because the fixture has vocab+replacements and the empty
        # sidecar gets reconciled from disk (expected and correct).
        assert app.state.config_dirty is False


async def test_changing_key_is_restart_sensitive(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        baseline = config.load(cfg)

        pane.query_one("#settings-hotkey-key", Input).value = "F13"
        await pilot.pause()

        assert str(app.state.doc["hotkey"]["key"]) == "F13"
        diff = config.diff_restart_sensitive(baseline, app.state.doc)
        assert "hotkey.key" in diff


async def test_toggling_modifier_updates_array(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        # Enable LEFTCTRL and LEFTALT
        pane.query_one("#settings-mod-leftctrl", Checkbox).value = True
        await pilot.pause()
        pane.query_one("#settings-mod-leftalt", Checkbox).value = True
        await pilot.pause()

        mods = [str(m) for m in app.state.doc["hotkey"]["modifiers"]]
        assert mods == ["LEFTCTRL", "LEFTALT"]

        # Turn LEFTCTRL off
        pane.query_one("#settings-mod-leftctrl", Checkbox).value = False
        await pilot.pause()
        mods = [str(m) for m in app.state.doc["hotkey"]["modifiers"]]
        assert mods == ["LEFTALT"]


async def test_modifier_change_is_restart_sensitive(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        baseline = config.load(cfg)

        pane.query_one("#settings-mod-leftshift", Checkbox).value = True
        await pilot.pause()

        diff = config.diff_restart_sensitive(baseline, app.state.doc)
        assert "hotkey.modifiers" in diff


async def test_mode_change(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-hotkey-mode", Select).value = "toggle"
        await pilot.pause()

        assert str(app.state.doc["hotkey"]["mode"]) == "toggle"
        assert app.state.dirty is True


async def test_modifier_checkboxes_render_with_visible_labels(tmp_env):
    """Guards against a regression where CSS forced Checkbox height to 1,
    which cropped Textual's `border: tall` and hid both the toggle glyph
    and the label — users saw four empty rectangles instead of labeled
    checkboxes. A Checkbox with the default 'tall' border renders at
    height 3; anything less means the content is being clipped."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        await pilot.pause()
        pane = app.query_one(SettingsPane)

        for mod in HOTKEY_MODIFIERS:
            cb = pane.query_one(f"#settings-mod-{mod.lower()}", Checkbox)
            assert cb.region.height >= 3, (
                f"Modifier checkbox for {mod} rendered at height "
                f"{cb.region.height}; need >= 3 for border + content"
            )
            assert cb.region.width > 0, (
                f"Modifier checkbox for {mod} has zero width"
            )


async def test_enabled_switch_persists_explicit_bool(tmp_env):
    """Our policy: writes explicit true/false, never 'commented-out = default'."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-hotkey-enabled", Switch).value = False
        await pilot.pause()

        assert app.state.doc["hotkey"]["enabled"] is False or \
               str(app.state.doc["hotkey"]["enabled"]) == "False" or \
               bool(app.state.doc["hotkey"]["enabled"]) is False

        await pilot.press("ctrl+s")
        await pilot.pause()

    raw = cfg.read_text()
    assert "enabled = false" in raw
