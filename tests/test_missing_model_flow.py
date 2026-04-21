"""Tests for Step 6 — missing-model banner wired to the Models-tab
download pipeline, with the post-download "Set active" handoff.

Strategy: mock `ModelsPane._run_download` so tests don't need a real
`voxtype` binary. Two modes:

  * success: creates `ggml-<name>.bin` in the models dir → reader's
    `_model_file_present` check passes post-download → banner advances
    to the "downloaded" stage.
  * failure: skips file creation → banner reverts to "missing".
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest
import tomlkit

from voxtype_tui import sidecar as sidecar_mod, sync, voxtype_cli
from voxtype_tui.app import MissingModelBanner, VoxtypeTUI
from voxtype_tui.models import ModelsPane

from .conftest import FIXTURES


# ---------------------------------------------------------------------------
# Fixture scaffolding
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    sync_p = tmp_path / "voxtype-tui" / "sync.json"
    sync_p.parent.mkdir()
    models = tmp_path / "models"
    shutil.copy(FIXTURES / "stock.toml", cfg)

    monkeypatch.setattr(sync, "SYNC_PATH", sync_p)
    monkeypatch.setattr(sync, "DEFAULT_MODELS_DIR", models)

    async def _inactive():
        return False

    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)
    return {"cfg": cfg, "side": side, "sync": sync_p, "models": models}


def _force_old_mtime(p: Path) -> None:
    if not p.exists():
        return
    past = time.time() - 600
    os.utime(p, (past, past))


def _write_sync_with_model(tmp_env, model: str) -> None:
    """Plant a sync.json newer than local that applies `whisper.model`."""
    _force_old_mtime(tmp_env["cfg"])
    if tmp_env["side"].exists():
        _force_old_mtime(tmp_env["side"])
    sync_block = {
        "vocabulary": [],
        "replacements": [],
        "settings": {"whisper": {"model": model}},
    }
    bundle = {
        "schema_version": 1,
        "format": sync.FORMAT_TAG,
        "generated_at": "2999-06-01T00:00:00Z",
        "generated_by_device": "zeus-laptop",
        "local_sync_hash": sync.stable_hash(sync_block),
        "sync": sync_block,
        "local": {},
    }
    tmp_env["sync"].write_text(json.dumps(bundle, indent=2))


def _stub_download_success(name: str, models_dir: Path):
    """Monkeypatch factory: a successful `_run_download` writes the
    expected model file so the post-download existence check passes."""
    async def impl(self, model_name):
        # Honor the engine guard from the real pipeline (we assert it's
        # whisper so that guard passes in the tests).
        assert self._current_engine() == "whisper"
        assert model_name == name
        models_dir.mkdir(parents=True, exist_ok=True)
        (models_dir / f"ggml-{name}.bin").write_bytes(b"simulated")
    return impl


def _stub_download_failure(name: str):
    async def impl(self, model_name):
        # No-op: model file never appears.
        pass
    return impl


# ---------------------------------------------------------------------------
# Banner stages — widget-level
# ---------------------------------------------------------------------------

async def test_banner_stages_render_correctly(tmp_env) -> None:
    """Exercise the three render states directly: missing → downloading
    → downloaded. The app-level handlers live in VoxtypeTUI; this test
    only verifies the widget's own set_* methods."""
    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one(MissingModelBanner)

        banner.set_missing("tiny")
        await pilot.pause()
        from textual.widgets import Button
        dl = banner.query_one("#missing-download", Button)
        sa = banner.query_one("#missing-set-active", Button)
        assert "hidden" not in dl.classes
        assert dl.disabled is False
        assert str(dl.label) == "Download"
        assert "hidden" in sa.classes

        banner.set_downloading("tiny")
        await pilot.pause()
        assert dl.disabled is True
        assert "Downloading" in str(dl.label)
        assert "hidden" in sa.classes

        banner.set_downloaded("tiny")
        await pilot.pause()
        assert "hidden" in dl.classes
        assert "hidden" not in sa.classes


async def test_banner_dismiss_hides(tmp_env) -> None:
    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one(MissingModelBanner)
        banner.set_missing("tiny")
        await pilot.pause()
        await pilot.click("#missing-dismiss")
        await pilot.pause()
        assert banner.display is False


# ---------------------------------------------------------------------------
# Download handoff — Banner → App → ModelsPane
# ---------------------------------------------------------------------------

async def test_download_button_advances_to_downloaded_stage(
    tmp_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full end-to-end: sync file names a missing model → startup puts
    banner in missing stage → Download click runs (mocked) pipeline →
    banner advances to downloaded."""
    _write_sync_with_model(tmp_env, "tiny")
    monkeypatch.setattr(
        ModelsPane, "_run_download",
        _stub_download_success("tiny", tmp_env["models"]),
    )

    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one(MissingModelBanner)
        assert banner.display is True
        assert banner.model_name == "tiny"

        await pilot.click("#missing-download")
        # Wait for the async download task to run.
        for _ in range(20):
            await pilot.pause()
            if (tmp_env["models"] / "ggml-tiny.bin").exists():
                break
        await pilot.pause()

        from textual.widgets import Button
        sa = banner.query_one("#missing-set-active", Button)
        assert "hidden" not in sa.classes
        assert banner.display is True


async def test_download_switches_to_models_tab_for_progress(
    tmp_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Progress lives in the Models tab's RichLog — tab must switch so
    the user actually sees it during the download."""
    _write_sync_with_model(tmp_env, "tiny")
    monkeypatch.setattr(
        ModelsPane, "_run_download",
        _stub_download_success("tiny", tmp_env["models"]),
    )

    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#missing-download")
        for _ in range(10):
            await pilot.pause()
        from textual.widgets import TabbedContent
        tabs = app.query_one("#tabs", TabbedContent)
        assert tabs.active == "models"


async def test_download_failure_resets_banner_to_missing(
    tmp_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Download subprocess exits without creating the model file →
    banner reverts to "missing" so the user can retry."""
    _write_sync_with_model(tmp_env, "tiny")
    monkeypatch.setattr(
        ModelsPane, "_run_download",
        _stub_download_failure("tiny"),
    )

    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one(MissingModelBanner)
        await pilot.click("#missing-download")
        for _ in range(10):
            await pilot.pause()

        # The Download button returned to the "Download" (retry) label,
        # set-active is hidden.
        from textual.widgets import Button
        dl = banner.query_one("#missing-download", Button)
        sa = banner.query_one("#missing-set-active", Button)
        assert str(dl.label) == "Download"
        assert dl.disabled is False
        assert "hidden" in sa.classes


async def test_download_raising_exception_is_caught_and_banner_resets(
    tmp_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A download that raises (e.g. voxtype binary missing) must not
    crash the UI — app catches, banner resets, toast explains."""
    _write_sync_with_model(tmp_env, "tiny")

    async def raising(self, name):
        raise RuntimeError("simulated voxtype-missing failure")

    monkeypatch.setattr(ModelsPane, "_run_download", raising)

    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one(MissingModelBanner)
        await pilot.click("#missing-download")
        for _ in range(10):
            await pilot.pause()

        from textual.widgets import Button
        dl = banner.query_one("#missing-download", Button)
        assert dl.disabled is False  # ready for retry
        assert str(dl.label) == "Download"


# ---------------------------------------------------------------------------
# Set active — flips whisper.model + dirties config
# ---------------------------------------------------------------------------

async def test_set_active_flips_whisper_model_and_dirties(
    tmp_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[Set active] writes whisper.model = name to the shadow state,
    flipping config_dirty. whisper.model ∈ RESTART_SENSITIVE_PATHS so
    the subsequent save path fires RestartModal — that specific
    behavior is covered by test_app_shell + the existing save tests;
    we only verify the state mutation here."""
    _write_sync_with_model(tmp_env, "large-v3-turbo")
    monkeypatch.setattr(
        ModelsPane, "_run_download",
        _stub_download_success("large-v3-turbo", tmp_env["models"]),
    )

    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        # Sync reconcile already wrote whisper.model = large-v3-turbo
        # into config (the APPLY step of reconcile). To isolate the
        # "Set active" post-download path, reset the model to a
        # different value first, then trigger download + set-active.
        app.state.set_setting("whisper.model", "tiny")
        app.state.config_dirty = False  # simulate post-save clean state

        await pilot.click("#missing-download")
        for _ in range(20):
            await pilot.pause()
            if (tmp_env["models"] / "ggml-large-v3-turbo.bin").exists():
                break
        await pilot.pause()

        # Click Set active.
        await pilot.click("#missing-set-active")
        await pilot.pause()

        assert str(app.state.doc["whisper"]["model"]) == "large-v3-turbo"
        assert app.state.config_dirty is True
        # Banner dismissed after set-active.
        assert app.query_one(MissingModelBanner).display is False


async def test_set_active_without_missing_model_noop(tmp_env) -> None:
    """Safety: clicking Set active when the banner has no model_name
    (shouldn't happen in practice, but belt-and-suspenders) is a no-op
    and doesn't crash."""
    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one(MissingModelBanner)
        # Force-display the banner with no model name — simulates a
        # bug scenario.
        banner.display = True
        # Directly invoke the button handler path without posting a real
        # button press, since the button is .hidden on the missing stage.
        app._apply_missing_model_set_active(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Banner dismissibility — user can defer without taking action
# ---------------------------------------------------------------------------

async def test_dismiss_in_missing_stage(
    tmp_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_sync_with_model(tmp_env, "tiny")
    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one(MissingModelBanner)
        assert banner.display is True
        await pilot.click("#missing-dismiss")
        await pilot.pause()
        assert banner.display is False


async def test_dismiss_in_downloaded_stage(
    tmp_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_sync_with_model(tmp_env, "tiny")
    monkeypatch.setattr(
        ModelsPane, "_run_download",
        _stub_download_success("tiny", tmp_env["models"]),
    )
    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one(MissingModelBanner)
        await pilot.click("#missing-download")
        for _ in range(20):
            await pilot.pause()
            if (tmp_env["models"] / "ggml-tiny.bin").exists():
                break
        await pilot.pause()

        from textual.widgets import Button
        sa = banner.query_one("#missing-set-active", Button)
        assert "hidden" not in sa.classes

        await pilot.click("#missing-dismiss")
        await pilot.pause()
        assert banner.display is False
