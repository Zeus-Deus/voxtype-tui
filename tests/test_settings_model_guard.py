"""Not-downloaded guard on the Settings tab's Model dropdown.

The Models tab's [Set active] and the download-complete promotion both
refuse to activate a model whose file isn't on disk — but the Settings
dropdown used to write `whisper.model` unguarded, so picking e.g.
`large-v3` before downloading it crash-looped the daemon at the
post-save restart (whisper.cpp exits on a missing model file). These
tests pin the third path closed.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from textual.widgets import Select

from voxtype_tui import voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.settings import SettingsPane
from .conftest import FIXTURES


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)

    # Only base.en and tiny.en are "downloaded"; large-v3 is not.
    models_dir = tmp_path / "voxtype-models"
    models_dir.mkdir()
    (models_dir / "ggml-base.en.bin").write_bytes(b"x" * 1000)
    (models_dir / "ggml-tiny.en.bin").write_bytes(b"x" * 1000)
    from voxtype_tui import models as models_mod
    monkeypatch.setattr(models_mod, "MODELS_DIR", models_dir)

    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)
    return cfg, side, models_dir


async def _goto_settings(pilot, app):
    await pilot.press("3")
    await pilot.pause()
    assert app.query_one("#tabs").active == "settings"


async def test_uninstalled_pick_is_rejected_and_reverted(tmp_env):
    """Picking a not-downloaded model must not write to state, and the
    dropdown must snap back to the current (installed) model."""
    cfg, side, models_dir = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        select = pane.query_one("#settings-model", Select)
        assert select.value == "base.en"  # stock

        select.value = "large-v3"  # in the catalog, NOT on disk
        await pilot.pause()

        # State untouched — this is the line the daemon crash-loop
        # depended on.
        assert str(app.state.doc["whisper"]["model"]) == "base.en"
        assert app.state.dirty is False
        # Dropdown snapped back.
        assert select.value == "base.en"


async def test_installed_pick_still_writes(tmp_env):
    cfg, side, models_dir = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-model", Select).value = "tiny.en"
        await pilot.pause()

        assert str(app.state.doc["whisper"]["model"]) == "tiny.en"
        assert app.state.dirty is True


async def test_rejected_pick_can_be_followed_by_valid_pick(tmp_env):
    """The revert's programmatic-change bookkeeping must not swallow the
    user's next real selection."""
    cfg, side, models_dir = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)
        select = pane.query_one("#settings-model", Select)

        select.value = "large-v3"
        await pilot.pause()
        assert select.value == "base.en"

        select.value = "tiny.en"
        await pilot.pause()
        assert str(app.state.doc["whisper"]["model"]) == "tiny.en"


async def test_dropdown_labels_mark_uninstalled_models(tmp_env):
    """Catalog models without an on-disk file are labeled so the user can
    see the state before committing to a pick."""
    cfg, side, models_dir = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        labels = {value: str(label) for label, value in pane._model_options("whisper")}
        assert labels["base.en"] == "base.en"
        assert labels["large-v3"] == "large-v3 (not downloaded)"


async def test_download_then_pick_succeeds(tmp_env):
    """Once the file lands on disk, the same pick goes through — the guard
    checks live state, not a cached snapshot."""
    cfg, side, models_dir = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)
        select = pane.query_one("#settings-model", Select)

        select.value = "large-v3"
        await pilot.pause()
        assert str(app.state.doc["whisper"]["model"]) == "base.en"

        (models_dir / "ggml-large-v3.bin").write_bytes(b"x" * 1000)
        select.value = "large-v3"
        await pilot.pause()
        assert str(app.state.doc["whisper"]["model"]) == "large-v3"
