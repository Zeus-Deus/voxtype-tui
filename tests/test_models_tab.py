"""Models tab — catalog rendering, set-active, delete (with active-model
guard), download gating on dirty state, cancel cleanup, refresh."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from textual.widgets import Button, DataTable, Select

from voxtype_tui import config, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.models import (
    MODEL_CATALOG,
    ConfirmDeleteModal,
    ModelsPane,
    humanize_bytes,
    parse_percent,
    strip_ansi,
)
from .conftest import FIXTURES


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    models_dir = tmp_path / "voxtype-models"
    models_dir.mkdir()
    shutil.copy(FIXTURES / "stock.toml", cfg)

    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)

    # Re-target MODELS_DIR at the temp dir so tests don't see the user's real
    # downloaded models (and deletes can't hit anything real).
    from voxtype_tui import models as models_mod
    monkeypatch.setattr(models_mod, "MODELS_DIR", models_dir)
    return cfg, side, models_dir


async def _goto_models(pilot, app):
    await pilot.press("4")
    await pilot.pause()
    assert app.query_one("#tabs").active == "models"


# ---- unit tests ----

def test_strip_ansi() -> None:
    assert strip_ansi("\x1b[32m✓\x1b[0m OK") == "✓ OK"


def test_parse_percent_picks_last_match() -> None:
    # Voxtype's curl-style output has both the hash bar and a trailing pct;
    # multiple pcts may exist on a carriage-returned line.
    assert parse_percent("####### 8.0%#################           24.5%") == 24.5
    assert parse_percent("100.0%") == 100.0


def test_parse_percent_returns_none_on_no_match() -> None:
    assert parse_percent("downloading…") is None


def test_humanize_bytes() -> None:
    assert humanize_bytes(0) == "0 B"
    assert humanize_bytes(1500) == "1 KB"
    assert humanize_bytes(150 * 1024 * 1024) == "150 MB"
    assert humanize_bytes(2 * 1024 * 1024 * 1024) == "2 GB"


def test_catalog_has_whisper_entries() -> None:
    assert MODEL_CATALOG["whisper"]
    names = [m.name for m in MODEL_CATALOG["whisper"]]
    assert "base.en" in names
    assert "large-v3-turbo" in names


# ---- UI tests ----

async def test_table_renders_catalog_with_downloaded_marks(tmp_env):
    cfg, side, models_dir = tmp_env
    # Pretend base.en and tiny.en exist on disk
    (models_dir / "ggml-base.en.bin").write_bytes(b"x" * 1000)
    (models_dir / "ggml-tiny.en.bin").write_bytes(b"x" * 1000)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_models(pilot, app)
        pane = app.query_one(ModelsPane)
        table = pane.query_one(DataTable)

        assert table.row_count >= len(MODEL_CATALOG["whisper"])
        # The active model (base.en from stock) should be marked with ●
        found_active = False
        for r in range(table.row_count):
            row = table.get_row_at(r)
            if str(row[0]) == "base.en":
                assert str(row[2]) == "downloaded"
                assert "●" in str(row[3])
                found_active = True
        assert found_active


async def test_set_active_writes_model_path_and_marks_dirty(tmp_env):
    cfg, side, models_dir = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_models(pilot, app)
        pane = app.query_one(ModelsPane)
        table = pane.query_one(DataTable)
        # Move cursor to large-v3-turbo
        for r in range(table.row_count):
            if str(table.get_row_at(r)[0]) == "large-v3-turbo":
                table.move_cursor(row=r)
                break
        await pilot.pause()

        btn = pane.query_one("#models-set-active", Button)
        btn.post_message(Button.Pressed(btn))
        await pilot.pause()

        assert str(app.state.doc["whisper"]["model"]) == "large-v3-turbo"
        assert app.state.config_dirty is True

        baseline = config.load(cfg)
        # whisper.model is restart-sensitive — save flow will trigger modal
        diff = config.diff_restart_sensitive(baseline, app.state.doc)
        assert "whisper.model" in diff


async def test_download_is_blocked_when_dirty(tmp_env):
    """The Download button must not spawn a subprocess when state.dirty,
    because voxtype writes config.toml mid-download and would clobber
    pending edits."""
    cfg, side, models_dir = tmp_env

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    calls: list[list[str]] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(list(args))
        class P:
            stdout = None
            async def wait(self):
                return 0
        return P()

    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_models(pilot, app)
        pane = app.query_one(ModelsPane)

        # Make the state dirty
        app.state.add_vocab("Placeholder")
        app.refresh_dirty()
        await pilot.pause()

        table = pane.query_one(DataTable)
        # Pick any row
        table.move_cursor(row=0)
        await pilot.pause()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            btn = pane.query_one("#models-download", Button)
            btn.post_message(Button.Pressed(btn))
            await pilot.pause()

        assert calls == [], "Download must not run while state is dirty"


async def test_delete_blocked_for_active_model(tmp_env):
    cfg, side, models_dir = tmp_env
    # Create the active model's file on disk
    (models_dir / "ggml-base.en.bin").write_bytes(b"x" * 1000)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_models(pilot, app)
        pane = app.query_one(ModelsPane)
        table = pane.query_one(DataTable)

        # Move cursor onto base.en (the active model)
        for r in range(table.row_count):
            if str(table.get_row_at(r)[0]) == "base.en":
                table.move_cursor(row=r)
                break
        await pilot.pause()

        btn = pane.query_one("#models-delete", Button)
        btn.post_message(Button.Pressed(btn))
        await pilot.pause()

        # File must still exist — delete was blocked
        assert (models_dir / "ggml-base.en.bin").exists()


async def test_delete_confirm_modal_removes_file(tmp_env):
    cfg, side, models_dir = tmp_env
    target = models_dir / "ggml-medium.en.bin"
    target.write_bytes(b"x" * 5000)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_models(pilot, app)
        pane = app.query_one(ModelsPane)
        table = pane.query_one(DataTable)

        for r in range(table.row_count):
            if str(table.get_row_at(r)[0]) == "medium.en":
                table.move_cursor(row=r)
                break
        await pilot.pause()

        btn = pane.query_one("#models-delete", Button)
        btn.post_message(Button.Pressed(btn))
        await pilot.pause()

        # A ConfirmDeleteModal is now pushed; press y to confirm
        assert isinstance(app.screen, ConfirmDeleteModal)
        await pilot.press("y")
        await pilot.pause()

        assert not target.exists()


async def test_delete_cancel_keeps_file(tmp_env):
    cfg, side, models_dir = tmp_env
    target = models_dir / "ggml-small.en.bin"
    target.write_bytes(b"x" * 5000)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_models(pilot, app)
        pane = app.query_one(ModelsPane)
        table = pane.query_one(DataTable)
        for r in range(table.row_count):
            if str(table.get_row_at(r)[0]) == "small.en":
                table.move_cursor(row=r)
                break
        await pilot.pause()

        btn = pane.query_one("#models-delete", Button)
        btn.post_message(Button.Pressed(btn))
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()

        assert target.exists()


async def test_engine_switch_rebuilds_table(tmp_env):
    cfg, side, models_dir = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_models(pilot, app)
        pane = app.query_one(ModelsPane)
        table = pane.query_one(DataTable)

        whisper_rows = table.row_count

        pane.query_one("#models-engine", Select).value = "parakeet"
        await pilot.pause()

        names = [str(table.get_row_at(r)[0]) for r in range(table.row_count)]
        assert any("parakeet" in n for n in names)
        assert all("whisper" not in n for n in names)


async def test_unknown_bin_files_are_surfaced(tmp_env):
    """A .bin file in the models dir that doesn't match the catalog shows
    up as 'unknown' so the user can manage it via the Delete button."""
    cfg, side, models_dir = tmp_env
    (models_dir / "ggml-rogue-model.bin").write_bytes(b"x" * 100)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_models(pilot, app)
        pane = app.query_one(ModelsPane)
        table = pane.query_one(DataTable)

        found = False
        for r in range(table.row_count):
            if str(table.get_row_at(r)[0]) == "rogue-model":
                assert str(table.get_row_at(r)[2]) == "unknown"
                found = True
                break
        assert found


async def test_disk_usage_reflects_file_sizes(tmp_env):
    cfg, side, models_dir = tmp_env
    (models_dir / "ggml-tiny.en.bin").write_bytes(b"x" * (5 * 1024 * 1024))

    from voxtype_tui.models import total_disk_usage

    used = total_disk_usage()
    assert used == 5 * 1024 * 1024

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_models(pilot, app)
        pane = app.query_one(ModelsPane)
        # The label widget exists and was updated during sync_from_state
        assert pane.query_one("#models-disk") is not None
