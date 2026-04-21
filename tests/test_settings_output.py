"""Output section of the Settings pane — mode, auto-submit, smart-auto-submit,
spoken punctuation, fallback-to-clipboard, type delay. None are restart-
sensitive; voxtype re-reads these per-transcription."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from textual.widgets import Input, Select, Switch

from voxtype_tui import config, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.settings import OUTPUT_MODES, SettingsPane
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


async def test_output_hydrates_from_stock(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        # stock: mode=type, fallback_to_clipboard=true, type_delay_ms=0
        assert pane.query_one("#settings-output-mode", Select).value == "type"
        assert pane.query_one("#settings-output-fallback", Switch).value is True
        assert pane.query_one("#settings-output-auto-submit", Switch).value is False
        assert pane.query_one("#settings-output-type-delay", Input).value == "0"
        # text section is fully commented in stock → switches default False
        assert pane.query_one("#settings-text-smart-auto-submit", Switch).value is False
        assert pane.query_one("#settings-text-spoken-punctuation", Switch).value is False
        assert app.state.config_dirty is False


async def test_output_mode_change(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-output-mode", Select).value = "clipboard"
        await pilot.pause()
        assert str(app.state.doc["output"]["mode"]) == "clipboard"

        # Not restart-sensitive — voxtype reads this per-transcription
        baseline = config.load(cfg)
        diff = config.diff_restart_sensitive(baseline, app.state.doc)
        assert diff == []


async def test_output_switches_all_toggle_and_persist(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-output-auto-submit", Switch).value = True
        pane.query_one("#settings-text-smart-auto-submit", Switch).value = True
        pane.query_one("#settings-text-spoken-punctuation", Switch).value = True
        pane.query_one("#settings-output-fallback", Switch).value = False
        await pilot.pause()

        assert bool(app.state.doc["output"]["auto_submit"]) is True
        assert bool(app.state.doc["text"]["smart_auto_submit"]) is True
        assert bool(app.state.doc["text"]["spoken_punctuation"]) is True
        assert bool(app.state.doc["output"]["fallback_to_clipboard"]) is False

        await pilot.press("ctrl+s")
        await pilot.pause()

    # Explicit bools land in the TOML (never leave them "commented-out = default")
    raw = cfg.read_text()
    assert "auto_submit = true" in raw
    assert "smart_auto_submit = true" in raw
    assert "spoken_punctuation = true" in raw
    assert "fallback_to_clipboard = false" in raw


async def test_type_delay_int_parse(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        inp = pane.query_one("#settings-output-type-delay", Input)
        inp.value = "5"
        await pilot.pause()
        assert int(app.state.doc["output"]["type_delay_ms"]) == 5

        inp.value = "not a number"
        await pilot.pause()
        # Still 5 — invalid is a no-op
        assert int(app.state.doc["output"]["type_delay_ms"]) == 5

        inp.value = "-1"
        await pilot.pause()
        # Negative rejected
        assert int(app.state.doc["output"]["type_delay_ms"]) == 5


async def test_output_mode_options_include_all_three(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)
        select = pane.query_one("#settings-output-mode", Select)
        # Textual Select expose _options or similar — safer to just try each
        for mode in OUTPUT_MODES:
            select.value = mode
            await pilot.pause()
            assert str(app.state.doc["output"]["mode"]) == mode
