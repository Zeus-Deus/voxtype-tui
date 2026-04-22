"""Engine & Model section of the Settings pane.

Covers: initial hydration from state, engine change preserves a model if it's
valid for the new engine, engine change resets the model otherwise, custom
model path round-trip, and language edits.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from textual.widgets import Input, Select

from voxtype_tui import config, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.settings import (
    CUSTOM_MODEL,
    MODELS_PER_ENGINE,
    MODEL_PATH_PER_ENGINE,
    SettingsPane,
)
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


async def test_initial_hydration_from_stock_config(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)
        assert pane.query_one("#settings-engine", Select).value == "whisper"
        # Stock has model = base.en
        assert pane.query_one("#settings-model", Select).value == "base.en"
        # Custom-path row stays hidden
        assert pane.query_one("#settings-custom-model-row").has_class("hidden")
        # Language from stock
        assert pane.query_one("#settings-language", Input).value == "en"
        # Nothing got dirty from the mount
        assert app.state.dirty is False


async def test_hydration_recognizes_custom_model_path(tmp_path, monkeypatch):
    """A config with a .bin path in whisper.model should arrive in Settings
    with the Model Select showing 'Custom…' and the path surfaced."""
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)

    # Inject a custom .bin path ahead of Settings mount
    doc = config.load(cfg)
    doc["whisper"]["model"] = "/opt/models/ggml-special.bin"
    config.save_atomic(doc, cfg)

    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)
        assert pane.query_one("#settings-model", Select).value == CUSTOM_MODEL
        assert not pane.query_one("#settings-custom-model-row").has_class("hidden")
        assert pane.query_one("#settings-custom-model", Input).value == "/opt/models/ggml-special.bin"
        assert app.state.dirty is False


async def test_engine_change_preserves_model_when_valid(tmp_env):
    """whisper/moonshine both list 'base' and 'tiny'. Switching engines from
    whisper to moonshine while model is 'base' should keep 'base', not reset."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        # Set whisper.model = "base"
        pane.query_one("#settings-model", Select).value = "base"
        await pilot.pause()
        assert app.state.doc["whisper"]["model"] == "base"

        # Flip engine to moonshine. "base" is valid there too.
        pane.query_one("#settings-engine", Select).value = "moonshine"
        await pilot.pause()

        assert app.state.doc["engine"] == "moonshine"
        assert "base" in MODELS_PER_ENGINE["moonshine"]
        # Moonshine's model lives at moonshine.model
        assert app.state.doc["moonshine"]["model"] == "base"
        assert pane.query_one("#settings-model", Select).value == "base"


async def test_engine_change_does_not_write_default_model(tmp_env):
    """Flipping engine must NOT silently write a fallback model into the
    new engine's config slot — the previous behavior clobbered any model
    the user might already have set, and was one of the contributing
    causes of the "model changes by itself" bug. The dropdown options
    refresh; the model field stays unset until the user picks
    explicitly. Voxtype's own default kicks in at daemon start when the
    key is absent, so an unset model is safe."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-model", Select).value = "large-v3-turbo"
        await pilot.pause()

        pane.query_one("#settings-engine", Select).value = "parakeet"
        await pilot.pause()

        assert app.state.doc["engine"] == "parakeet"
        # Engine flipped, but no parakeet.model was written. The user
        # picks in the now-repopulated dropdown.
        parakeet_section = app.state.doc.get("parakeet")
        if parakeet_section is not None:
            assert "model" not in parakeet_section, (
                "engine flip must not auto-write parakeet.model — that was "
                "the silent-overwrite bug"
            )


async def test_model_change_is_restart_sensitive(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        baseline = config.load(cfg)

        pane.query_one("#settings-model", Select).value = "tiny.en"
        await pilot.pause()

        diff = config.diff_restart_sensitive(baseline, app.state.doc)
        assert "whisper.model" in diff


async def test_custom_path_roundtrip(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-model", Select).value = CUSTOM_MODEL
        await pilot.pause()

        assert not pane.query_one("#settings-custom-model-row").has_class("hidden")

        custom_input = pane.query_one("#settings-custom-model", Input)
        custom_input.value = "/home/tester/my-model.bin"
        await pilot.pause()

        assert app.state.doc["whisper"]["model"] == "/home/tester/my-model.bin"
        assert app.state.config_dirty is True


async def test_language_edit_persists(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        lang = pane.query_one("#settings-language", Input)
        lang.value = "de"
        await pilot.pause()

        assert app.state.doc["whisper"]["language"] == "de"
        assert app.state.config_dirty is True


async def test_engine_change_does_not_touch_other_engine_paths(tmp_env):
    """When flipping whisper → parakeet, the old whisper.model value is left
    alone (voxtype just ignores inactive-engine sections). Don't silently
    delete user data."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        # baseline whisper.model is stock 'base.en'
        assert app.state.doc["whisper"]["model"] == "base.en"

        pane.query_one("#settings-engine", Select).value = "parakeet"
        await pilot.pause()

        assert app.state.doc["whisper"]["model"] == "base.en"
        assert app.state.doc["engine"] == "parakeet"


async def test_save_flow_from_settings(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-language", Input).value = "fr"
        await pilot.pause()
        assert app.state.dirty

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not app.state.dirty

    reloaded = config.load(cfg)
    assert str(reloaded["whisper"]["language"]) == "fr"
