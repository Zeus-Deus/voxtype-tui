"""Tests for the Download button on non-whisper engines.

Non-whisper download uses `app.suspend()` + spawning Voxtype's own
interactive picker (`voxtype setup model`) because `setup --download
--model <NAME>` only knows whisper names. We cannot unit-test the
interactive flow end-to-end — no real terminal in pytest — so we test
the wiring: dispatch routes correctly, the subprocess call is made,
failures are surfaced, state is reloaded on success.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from voxtype_tui import config, sidecar, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.models import ModelsPane

from .conftest import FIXTURES


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)
    from voxtype_tui import models as models_mod
    models_dir = tmp_path / "voxtype-models"
    models_dir.mkdir()
    monkeypatch.setattr(models_mod, "MODELS_DIR", models_dir)
    return cfg, side, models_dir


async def _switch_to_engine(pilot, app, engine: str) -> None:
    """Move the Models-tab engine Select to a specific engine."""
    await pilot.press("4")  # Models tab
    await pilot.pause()
    from textual.widgets import Select
    pane = app.query_one(ModelsPane)
    pane.query_one("#models-engine", Select).value = engine
    await pilot.pause()


# ---------------------------------------------------------------------------

async def test_non_whisper_dispatch_routes_to_picker(tmp_env, monkeypatch):
    """Pressing Download on a moonshine row must call the interactive-picker
    path, NOT the whisper-specific `_run_download`."""
    cfg, side, _ = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)

    picker_calls: list[str] = []
    download_calls: list[str] = []

    async def fake_picker(self):
        picker_calls.append("called")

    async def fake_download(self, name):
        download_calls.append(name)

    monkeypatch.setattr(ModelsPane, "_run_interactive_picker", fake_picker)
    monkeypatch.setattr(ModelsPane, "_run_download", fake_download)

    async with app.run_test() as pilot:
        await pilot.pause()
        await _switch_to_engine(pilot, app, "moonshine")
        pane = app.query_one(ModelsPane)
        pane._selected_model_name = lambda: "base"
        pane._action_download()
        # Let the scheduled task tick.
        for _ in range(5):
            await pilot.pause()

    assert picker_calls == ["called"]
    assert download_calls == []


async def test_whisper_dispatch_still_uses_direct_download(tmp_env, monkeypatch):
    """The whisper path must NOT regress to the interactive picker —
    users depend on the in-app progress bar for whisper downloads."""
    cfg, side, _ = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)

    picker_calls: list[str] = []
    download_calls: list[str] = []

    async def fake_picker(self):
        picker_calls.append("called")

    async def fake_download(self, name):
        download_calls.append(name)

    monkeypatch.setattr(ModelsPane, "_run_interactive_picker", fake_picker)
    monkeypatch.setattr(ModelsPane, "_run_download", fake_download)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("4")  # Models tab — defaults to whisper
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        pane._selected_model_name = lambda: "tiny.en"
        pane._action_download()
        for _ in range(5):
            await pilot.pause()

    assert download_calls == ["tiny.en"]
    assert picker_calls == []


async def test_picker_reloads_state_after_success(tmp_env, monkeypatch):
    """When the picker exits cleanly, we reload AppState from disk so
    any active-model change Voxtype made gets picked up. Without this
    the TUI's in-memory state would clobber the picker's change on
    the user's next save."""
    cfg, side, _ = tmp_env

    async def fake_to_thread(fn, *a, **k):
        # Simulate successful subprocess invocation.
        return 0

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/voxtype")

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    reload_calls: list[int] = []
    orig_load = VoxtypeTUI.load_state
    def counted_load(self):
        reload_calls.append(1)
        return orig_load(self)
    monkeypatch.setattr(VoxtypeTUI, "load_state", counted_load)

    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(reload_calls)
        pane = app.query_one(ModelsPane)
        await pane._run_interactive_picker()
        await pilot.pause()
        # load_state must have been called by the picker-return path.
        assert len(reload_calls) > before


async def test_picker_handles_missing_voxtype_binary(tmp_env, monkeypatch):
    """If `voxtype` isn't on PATH the picker path must bail with a
    clear error instead of raising."""
    cfg, side, _ = tmp_env
    monkeypatch.setattr(shutil, "which", lambda name: None)
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    notified: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        orig_notify = pane.app.notify
        def capture(msg, *a, **k):
            notified.append(msg)
            return orig_notify(msg, *a, **k)
        monkeypatch.setattr(pane.app, "notify", capture)
        await pane._run_interactive_picker()
        await pilot.pause()
    assert any("voxtype binary not on PATH" in m for m in notified)
