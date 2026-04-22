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
    split_terminal_output,
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


# ---- split_terminal_output (regression for the "download looks frozen" bug) ----


def test_split_terminal_output_lf_only() -> None:
    units, leftover = split_terminal_output(b"hello\nworld\n")
    assert units == [("hello", True), ("world", True)]
    assert leftover == b""


def test_split_terminal_output_cr_only_are_progress_frames() -> None:
    """Carriage-returns mid-stream are progress-bar overwrites and must be
    delivered as non-newline units so the UI updates the bar live rather
    than waiting for the eventual LF."""
    units, leftover = split_terminal_output(b"10.0%\r50.0%\r")
    assert units == [("10.0%", False), ("50.0%", False)]
    assert leftover == b""


def test_split_terminal_output_cr_lf_is_a_single_newline() -> None:
    units, leftover = split_terminal_output(b"line\r\nnext\n")
    assert units == [("line", True), ("next", True)]


def test_split_terminal_output_mixed_cr_then_lf_at_end() -> None:
    """Realistic voxtype download shape: progress frames separated by \r,
    then a final \n when the whole progress bar completes, then a
    subsequent 'Saved to ...' line."""
    raw = (
        b"######## 8.0%\r################ 24.5%\r"
        b"######################### 48.3%\r"
        b"######################################## 100.0%\n"
        b"Saved to /path/to/model.bin\n"
    )
    units, leftover = split_terminal_output(raw)
    texts_with_flag = [(t, nl) for t, nl in units]
    # The first three \r-terminated frames must be progress-only
    assert texts_with_flag[0][1] is False
    assert "8.0%" in texts_with_flag[0][0]
    assert texts_with_flag[1][1] is False
    assert "24.5%" in texts_with_flag[1][0]
    assert texts_with_flag[2][1] is False
    # The fourth (the terminal \n after the last \r-frame) is a real line
    assert texts_with_flag[3][1] is True
    assert "100.0%" in texts_with_flag[3][0]
    # And "Saved to …" is a real logged line
    assert texts_with_flag[4][1] is True
    assert "Saved to" in texts_with_flag[4][0]
    assert leftover == b""


def test_split_terminal_output_preserves_partial_trailing() -> None:
    """Chunks can land mid-line; partial bytes should come back as leftover
    so the next chunk can concatenate and complete the unit."""
    units, leftover = split_terminal_output(b"complete\nparti")
    assert units == [("complete", True)]
    assert leftover == b"parti"

    # Feeding the rest should produce the remaining line
    more_units, more_leftover = split_terminal_output(leftover + b"al\n")
    assert more_units == [("partial", True)]
    assert more_leftover == b""


def test_split_terminal_output_strips_ansi() -> None:
    units, _ = split_terminal_output(b"\x1b[32m\xe2\x9c\x93\x1b[0m OK\n")
    assert units == [("✓ OK", True)]


def test_split_terminal_output_empty_input() -> None:
    units, leftover = split_terminal_output(b"")
    assert units == []
    assert leftover == b""


def test_split_terminal_output_cr_followed_by_real_line() -> None:
    """Verifies the realistic sequence: final \r-overwrite followed by a
    normal \n-line emits one progress unit and one logged line."""
    raw = b"######## 100.0%\rDone\n"
    units, _ = split_terminal_output(raw)
    assert len(units) == 2
    assert units[0] == ("######## 100.0%", False)
    assert units[1] == ("Done", True)


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
    # Guard: Set Active requires the model file on disk.
    (models_dir / "ggml-large-v3-turbo.bin").write_bytes(b"x" * 1000)
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


async def test_download_progress_updates_live_not_at_end(tmp_env):
    """Regression for the 'download looks frozen, then jumps to 100%' bug.
    Feed a realistic voxtype-style stream through _run_download: three
    \\r-separated progress frames, then a final \\n-terminated completion
    line. The ProgressBar.progress must advance incrementally between
    chunks — not jump straight from 0 to 100 at the end."""
    cfg, side, models_dir = tmp_env

    # Build a fake async stdout that yields chunks like voxtype's progress
    # emission: bar frames end with \r, final success line ends with \n.
    chunks = [
        b"Downloading tiny.en...\n",
        b"######## 10.0%\r",
        b"################## 45.0%\r",
        b"########################## 80.0%\r",
        b"############################ 100.0%\n",
        b"  Saved to /tmp/ggml-tiny.en.bin\n",
        b"  Config updated to use 'tiny.en'\n",
    ]

    class _FakeStream:
        def __init__(self, parts):
            self._parts = list(parts)

        async def read(self, n):
            if not self._parts:
                return b""
            return self._parts.pop(0)

    class _FakeProc:
        def __init__(self, parts):
            self.stdout = _FakeStream(parts)

        async def wait(self):
            return 0

    # Monkeypatch create_subprocess_exec to return our fake
    import asyncio as _aio
    seen_progress: list[float] = []

    from voxtype_tui import models as models_mod

    async def fake_create(*args, **kwargs):
        return _FakeProc(chunks)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_aio, "create_subprocess_exec", fake_create)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            await _goto_models(pilot, app)
            pane = app.query_one(ModelsPane)

            # Intercept ProgressBar.update so we can verify it was called
            # multiple times with increasing values — not just once at the end.
            from textual.widgets import ProgressBar
            progress = pane.query_one("#models-progress", ProgressBar)
            original_update = progress.update

            def recording_update(*args, **kwargs):
                if "progress" in kwargs:
                    seen_progress.append(float(kwargs["progress"]))
                return original_update(*args, **kwargs)

            progress.update = recording_update  # type: ignore[method-assign]

            await pane._run_download("tiny.en")
            await pilot.pause()
    finally:
        monkeypatch.undo()

    # The bar must have been pushed through at least 10, 45, 80, and 100%
    # separately — not jumped straight to 100.
    non_zero = [p for p in seen_progress if p > 0]
    assert non_zero, f"ProgressBar never got a non-zero update: {seen_progress}"
    assert 10.0 in non_zero
    assert 45.0 in non_zero
    assert 80.0 in non_zero
    assert 100.0 in non_zero
    # At least four distinct intermediate values in order
    ascending_order = sorted(set(non_zero))
    assert ascending_order == [10.0, 45.0, 80.0, 100.0]


async def test_set_active_syncs_settings_model_dropdown(tmp_env):
    """Regression: setting a model in the Models tab must also refresh the
    Settings tab's Model dropdown — it used to stay pinned to whatever was
    loaded at mount, so the user saw two tabs disagreeing about the active
    model."""
    cfg, side, models_dir = tmp_env
    # Guard: Set Active requires the model file on disk.
    (models_dir / "ggml-large-v3-turbo.bin").write_bytes(b"x" * 1000)
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Settings mounts first so its widgets are hydrated with stock's
        # "base.en" before we touch the Models tab.
        await pilot.press("3")
        await pilot.pause()
        from voxtype_tui.settings import SettingsPane
        settings_pane = app.query_one(SettingsPane)
        assert settings_pane.query_one("#settings-model", Select).value == "base.en"

        await _goto_models(pilot, app)
        pane = app.query_one(ModelsPane)
        table = pane.query_one(DataTable)
        for r in range(table.row_count):
            if str(table.get_row_at(r)[0]) == "large-v3-turbo":
                table.move_cursor(row=r)
                break
        await pilot.pause()

        btn = pane.query_one("#models-set-active", Button)
        btn.post_message(Button.Pressed(btn))
        await pilot.pause()

        # state updated
        assert str(app.state.doc["whisper"]["model"]) == "large-v3-turbo"
        # Settings dropdown tracked the change without needing ctrl+r
        assert (
            settings_pane.query_one("#settings-model", Select).value
            == "large-v3-turbo"
        )


async def test_settings_model_change_syncs_models_tab_active_mark(tmp_env):
    """Reverse direction: editing the Model dropdown in Settings must move
    the ● active-mark in the Models tab's table without requiring a reload."""
    cfg, side, models_dir = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Prime the Models tab so its DataTable exists.
        await _goto_models(pilot, app)
        models_pane = app.query_one(ModelsPane)
        table = models_pane.query_one(DataTable)

        # Stock config has base.en active
        def active_row_name() -> str | None:
            for r in range(table.row_count):
                row = table.get_row_at(r)
                if "●" in str(row[3]):
                    return str(row[0])
            return None
        assert active_row_name() == "base.en"

        # Switch to Settings and change the Model
        await pilot.press("3")
        await pilot.pause()
        from voxtype_tui.settings import SettingsPane
        settings_pane = app.query_one(SettingsPane)
        settings_pane.query_one("#settings-model", Select).value = "small"
        await pilot.pause()

        # ● follows without a reload
        assert active_row_name() == "small"


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


async def test_progress_and_log_hidden_when_no_download_in_flight(tmp_env):
    """Regression: the ProgressBar (and the RichLog beneath it) used to
    render a stale "0%" bar even at rest, looking like a stuck UI
    element. They should only appear while a download is actually
    running."""
    cfg, side, _ = tmp_env
    from textual.widgets import ProgressBar, RichLog
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_models(pilot, app)
        pane = app.query_one(ModelsPane)
        progress = pane.query_one("#models-progress", ProgressBar)
        log = pane.query_one("#models-log", RichLog)
        assert progress.has_class("hidden"), \
            "ProgressBar must be hidden when no download is in flight"
        assert log.has_class("hidden"), \
            "RichLog must be hidden when no download is in flight"

        # Flipping the buttons-downloading state shows them; flipping
        # back hides them again.
        pane._set_buttons_downloading(True)
        await pilot.pause()
        assert not progress.has_class("hidden")
        assert not log.has_class("hidden")

        pane._set_buttons_downloading(False)
        await pilot.pause()
        assert progress.has_class("hidden")
        assert log.has_class("hidden")
