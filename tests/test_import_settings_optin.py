"""Tests for Fix #7 — Ctrl+I Import settings is opt-in.

The Ctrl+I import flow used to apply `bundle.sync.settings`
unconditionally. Importing an old `voxtype-tui-export-*.json` to
restore vocab silently overwrote `whisper.model` (one of the
contributing causes of the "model changes by itself" bug). Now the
import dialog has an "Include settings" checkbox that defaults OFF —
settings only apply when the user explicitly opts in.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from textual.widgets import Checkbox, Input

from voxtype_tui import sync, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.screens.import_bundle import ImportBundleModal
from voxtype_tui.state import AppState
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


def _make_bundle(tmp_path: Path, *, model: str = "tiny.en", vocab: list[str] | None = None) -> Path:
    """Write a voxtype-tui bundle file containing a settings change AND
    new vocab — so we can test that vocab merges while settings are
    gated behind the toggle."""
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps({
        "schema_version": 1,
        "format": sync.FORMAT_TAG,
        "generated_at": "2026-04-21T20:00:00Z",
        "generated_by_device": "test-device",
        "local_sync_hash": "0" * 64,
        "sync": {
            "vocabulary": [{"phrase": v} for v in (vocab or ["NewVocab"])],
            "replacements": [],
            "settings": {"whisper": {"model": model}},
        },
        "local": {},
    }))
    return bundle_path


async def test_import_default_keeps_local_settings(tmp_env, tmp_path):
    """Default (Include settings unchecked) → vocab merges, model stays."""
    cfg, side = tmp_env
    bundle_path = _make_bundle(tmp_path, model="tiny.en")
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        # baseline whisper.model from stock fixture is base.en
        baseline_model = str(app.state.doc["whisper"]["model"])

        modal = ImportBundleModal(app.state)
        await app.push_screen(modal)
        await pilot.pause()

        modal.query_one("#import-path", Input).value = str(bundle_path)
        modal.query_one("#do-load").press()
        await pilot.pause()

        # Include-settings toggle is now visible (bundle has settings)
        # and defaults to OFF.
        toggle = modal.query_one("#include-settings", Checkbox)
        assert not modal.query_one("#include-settings-row").has_class("hidden")
        assert toggle.value is False

        modal.query_one("#do-apply").press()
        await pilot.pause()

        # Vocab merged.
        assert any(v.phrase == "NewVocab" for v in app.state.sc.vocabulary)
        # Model preserved — settings were stripped before apply.
        assert str(app.state.doc["whisper"]["model"]) == baseline_model


async def test_import_with_settings_checked_applies_model(tmp_env, tmp_path):
    """Explicit Include settings ON → model overwrites as before."""
    cfg, side = tmp_env
    bundle_path = _make_bundle(tmp_path, model="tiny.en")
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = ImportBundleModal(app.state)
        await app.push_screen(modal)
        await pilot.pause()

        modal.query_one("#import-path", Input).value = str(bundle_path)
        modal.query_one("#do-load").press()
        await pilot.pause()

        modal.query_one("#include-settings", Checkbox).value = True
        await pilot.pause()

        modal.query_one("#do-apply").press()
        await pilot.pause()

        assert str(app.state.doc["whisper"]["model"]) == "tiny.en"


async def test_import_settings_row_hidden_when_bundle_has_no_settings(
    tmp_env, tmp_path,
):
    """A vocab-only bundle (Vexis vocabulary, etc.) doesn't show the
    Include-settings toggle — there's nothing to gate."""
    cfg, side = tmp_env
    bundle_path = tmp_path / "vocab-only.json"
    bundle_path.write_text(json.dumps({
        "schema_version": 1,
        "format": sync.FORMAT_TAG,
        "generated_at": "2026-04-21T20:00:00Z",
        "generated_by_device": "test-device",
        "local_sync_hash": "0" * 64,
        "sync": {
            "vocabulary": [{"phrase": "JustVocab"}],
            "replacements": [],
            "settings": {},
        },
        "local": {},
    }))

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = ImportBundleModal(app.state)
        await app.push_screen(modal)
        await pilot.pause()

        modal.query_one("#import-path", Input).value = str(bundle_path)
        modal.query_one("#do-load").press()
        await pilot.pause()

        assert modal.query_one("#include-settings-row").has_class("hidden")
