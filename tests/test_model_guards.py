"""Tests for the three install-check guards that prevent voxtype-tui from
pointing the daemon at a model file that isn't on disk.

  1. `_filter_uninstalled_models` — sync-reconcile pre-filter. The
     load-bearing fix for the 2026-04-22 incident where a sync.json
     overwrote `whisper.model = large-v3` with an older `base` that
     wasn't installed, causing the daemon to crash on restart.

  2. `ModelsPane._action_set_active` — refuses to set active a model
     the user hasn't downloaded yet.

  3. `_apply_missing_model_set_active` (the banner "download + set
     active" path) — refuses when the download subprocess exited but
     the file didn't materialize.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from voxtype_tui import models, sync


# ---------------------------------------------------------------------------
# Guard #1 — sync pre-filter
# ---------------------------------------------------------------------------

def _install(tmp_path: Path, engine: str, name: str) -> None:
    """Create the on-disk artifact for (engine, name) in tmp_path."""
    p = models.model_file_path(engine, name, models_dir=tmp_path)
    if engine == "whisper":
        p.write_bytes(b"x" * 100)
    else:
        p.mkdir(parents=True, exist_ok=True)
        (p / "marker").write_bytes(b"x" * 10)


def test_filter_keeps_installed_whisper_model(tmp_path: Path) -> None:
    _install(tmp_path, "whisper", "large-v3")
    bundle_sync = {"settings": {"whisper": {"model": "large-v3"}}}
    filtered, warnings, skipped = sync._filter_uninstalled_models(bundle_sync, tmp_path)
    assert filtered == bundle_sync  # unchanged
    assert warnings == []
    assert skipped == []


def test_filter_drops_missing_whisper_model(tmp_path: Path) -> None:
    """The incident scenario: bundle says 'base', we have 'large-v3',
    'base' isn't installed → drop the field, keep local."""
    _install(tmp_path, "whisper", "large-v3")
    bundle_sync = {"settings": {"whisper": {"model": "base"}, "engine": "whisper"}}
    filtered, warnings, skipped = sync._filter_uninstalled_models(bundle_sync, tmp_path)
    assert "model" not in filtered["settings"]["whisper"]
    assert filtered["settings"]["engine"] == "whisper"  # non-model fields preserved
    assert len(warnings) == 1
    assert "whisper.model" in warnings[0]
    assert "'base'" in warnings[0]
    assert skipped == [("whisper", "base")]


def test_filter_handles_all_seven_engines(tmp_path: Path) -> None:
    """Every engine with a `.model` field in the synced settings block
    gets filtered the same way."""
    bundle_sync = {"settings": {
        "whisper": {"model": "tiny.en"},
        "parakeet": {"model": "parakeet-tdt-0.6b-v3"},
        "moonshine": {"model": "base"},
        "sensevoice": {"model": "small"},
        "paraformer": {"model": "zh"},
        "dolphin": {"model": "base"},
        "omnilingual": {"model": "300m"},
    }}
    # Install only whisper + moonshine; everything else should be stripped.
    _install(tmp_path, "whisper", "tiny.en")
    _install(tmp_path, "moonshine", "base")
    filtered, warnings, skipped = sync._filter_uninstalled_models(bundle_sync, tmp_path)
    assert filtered["settings"]["whisper"]["model"] == "tiny.en"
    assert filtered["settings"]["moonshine"]["model"] == "base"
    assert "model" not in filtered["settings"]["parakeet"]
    assert "model" not in filtered["settings"]["sensevoice"]
    assert "model" not in filtered["settings"]["paraformer"]
    assert "model" not in filtered["settings"]["dolphin"]
    assert "model" not in filtered["settings"]["omnilingual"]
    # One warning per skipped engine.
    assert len(warnings) == 5
    assert len(skipped) == 5


def test_filter_preserves_non_model_fields(tmp_path: Path) -> None:
    """The filter must not touch vocab, replacements, or other settings."""
    bundle_sync = {
        "vocabulary": ["Codemux"],
        "replacements": [{"from": "cloud code", "to": "Claude Code"}],
        "settings": {
            "whisper": {"model": "base", "language": "en"},
            "text": {"spoken_punctuation": True},
        },
    }
    filtered, _, _ = sync._filter_uninstalled_models(bundle_sync, tmp_path)
    assert filtered["vocabulary"] == ["Codemux"]
    assert filtered["replacements"] == [{"from": "cloud code", "to": "Claude Code"}]
    assert filtered["settings"]["whisper"]["language"] == "en"
    assert filtered["settings"]["text"] == {"spoken_punctuation": True}


def test_filter_does_not_mutate_input(tmp_path: Path) -> None:
    """Caller's dict must stay unchanged (important because the bundle
    is shared with other reconcile steps downstream)."""
    bundle_sync = {"settings": {"whisper": {"model": "base"}}}
    original_dump = str(bundle_sync)
    sync._filter_uninstalled_models(bundle_sync, tmp_path)
    assert str(bundle_sync) == original_dump


def test_filter_noop_on_malformed_bundle(tmp_path: Path) -> None:
    """A garbage bundle (wrong types, missing keys) must not crash."""
    for bad in [{}, {"settings": None}, {"settings": {"whisper": "not-a-dict"}},
                {"settings": {"whisper": {"model": None}}},
                {"settings": {"whisper": {"model": ""}}}]:
        filtered, warnings, skipped = sync._filter_uninstalled_models(bad, tmp_path)
        assert warnings == []
        assert skipped == []


def test_filter_missing_models_dir_treats_everything_as_missing(tmp_path: Path) -> None:
    """Fresh install with no ~/.local/share/voxtype/models — every
    sync'd model looks missing, all get filtered. Correct behavior:
    we can't activate anything yet anyway."""
    missing = tmp_path / "never-created"
    bundle_sync = {"settings": {"whisper": {"model": "base"}}}
    filtered, warnings, skipped = sync._filter_uninstalled_models(bundle_sync, missing)
    assert "model" not in filtered["settings"]["whisper"]
    assert len(warnings) == 1
    assert skipped == [("whisper", "base")]


# ---------------------------------------------------------------------------
# Guard #2 — [Set active] refuses un-installed
#
# Uses Textual's Pilot to boot the full app against fixture files and
# drive the Models tab. Covers the real `_action_set_active` code path
# end-to-end — no stubbing of the pane itself.
# ---------------------------------------------------------------------------

async def test_set_active_refuses_uninstalled(tmp_path, monkeypatch):
    """Selecting a not-downloaded whisper model and pressing Enter must
    NOT write `whisper.model` to the doc. Toast warning fires instead."""
    import shutil
    from voxtype_tui import config, sidecar, voxtype_cli
    from voxtype_tui.app import VoxtypeTUI
    from voxtype_tui.models import ModelsPane
    from .conftest import FIXTURES

    monkeypatch.setattr(models, "MODELS_DIR", tmp_path / "voxtype-models")
    (tmp_path / "voxtype-models").mkdir()

    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        pane._selected_model_name = lambda: "medium.en"  # not installed
        pane._action_set_active()
        await pilot.pause()

        # The doc's whisper.model must not have flipped to the uninstalled value.
        whisper = app.state.doc.get("whisper")
        actual = str(whisper.get("model")) if whisper is not None and "model" in whisper else None
        assert actual != "medium.en"
        assert not app.state.config_dirty


async def test_set_active_allows_installed(tmp_path, monkeypatch):
    """Mirror of the above — with the file on disk, set-active proceeds."""
    import shutil
    from voxtype_tui import config, sidecar, voxtype_cli
    from voxtype_tui.app import VoxtypeTUI
    from voxtype_tui.models import ModelsPane
    from .conftest import FIXTURES

    models_dir = tmp_path / "voxtype-models"
    models_dir.mkdir()
    (models_dir / "ggml-tiny.en.bin").write_bytes(b"x" * 100)
    monkeypatch.setattr(models, "MODELS_DIR", models_dir)

    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        pane._selected_model_name = lambda: "tiny.en"
        pane._action_set_active()
        await pilot.pause()

        whisper = app.state.doc.get("whisper")
        assert str(whisper["model"]) == "tiny.en"
        assert app.state.config_dirty


# ---------------------------------------------------------------------------
# Guard #3 — banner "set active" refuses incomplete download
# ---------------------------------------------------------------------------

async def test_banner_set_active_refuses_if_file_missing(tmp_path, monkeypatch):
    """If the download subprocess exited without producing the file
    (curl non-zero, user cancelled), the banner's Set-Active button
    must not write a stale name to config."""
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path)
    import shutil
    from pathlib import Path as _P
    from voxtype_tui import config, sidecar, voxtype_cli
    from voxtype_tui.app import VoxtypeTUI
    from .conftest import FIXTURES

    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)

    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Simulate: user clicked banner's Set-Active for a model whose
        # file never landed on disk.
        app._apply_missing_model_set_active("large-v3")
        await pilot.pause()
        # In-memory state's whisper.model must NOT have been flipped to
        # the uninstalled "large-v3". The doc still reflects whatever
        # the fixture originally had (or nothing at all).
        whisper_block = app.state.doc.get("whisper")
        actual_model = (
            str(whisper_block.get("model")) if whisper_block is not None and
            "model" in whisper_block else None
        )
        assert actual_model != "large-v3"
