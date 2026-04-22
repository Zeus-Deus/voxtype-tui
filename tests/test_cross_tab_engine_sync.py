"""Tests for Fix #4 — Models tab engine-awareness.

The Models tab's engine `Select` was previously initialized once in
`on_mount` to the hardcoded literal `"whisper"` and never re-synced from
`state.doc["engine"]`. If the live engine was non-whisper, a Set Active
click wrote to `whisper.model` regardless — silently corrupting the
wrong engine's slot.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from textual.widgets import Select

from voxtype_tui import voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.models import ModelsPane
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


async def test_models_tab_picks_up_live_engine_on_mount(tmp_env):
    """With `engine = parakeet` in config, the Models tab's engine
    Select must land on parakeet — not the hardcoded 'whisper' fallback."""
    cfg, side = tmp_env
    # Top-level `engine = ...` must come BEFORE any section header,
    # otherwise it lands inside the previous section. Prepend.
    cfg.write_text('engine = "parakeet"\n' + cfg.read_text())
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("4")  # Models tab
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        assert str(pane.query_one("#models-engine", Select).value) == "parakeet"


async def test_models_tab_resyncs_engine_on_reload(tmp_env):
    """Ctrl+R path: engine Select re-syncs from state.doc after reload."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        assert str(pane.query_one("#models-engine", Select).value) == "whisper"

        # Simulate someone editing config.toml behind our back.
        cfg.write_text('engine = "moonshine"\n' + cfg.read_text())
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert str(pane.query_one("#models-engine", Select).value) == "moonshine"
