"""Tests for the startup reader, staleness compare, conflict detection,
and the three sync banners.

Unit tests drive `sync.reconcile_sync_on_startup` directly with
controlled filesystem fixtures. Pilot tests verify the banners render
as expected in the app shell.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest
import tomlkit

from voxtype_tui import sidecar as sidecar_mod, sync, voxtype_cli
from voxtype_tui.app import (
    AppliedSyncBanner,
    MissingModelBanner,
    SyncConflictBanner,
    VoxtypeTUI,
)
from voxtype_tui.state import AppState

from .conftest import FIXTURES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    """Minimal fixture: an isolated config + sidecar + sync directory."""
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    sync_dir = tmp_path / "voxtype-tui"
    sync_dir.mkdir()
    sync_p = sync_dir / "sync.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)
    return {
        "cfg": cfg,
        "side": side,
        "sync": sync_p,
        "models": tmp_path / "models",
    }


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Full app env: patches SYNC_PATH and DEFAULT_MODELS_DIR into
    tmp_path so the startup reader reads/writes under our control."""
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


def _write_sync_bundle(
    sync_path: Path,
    *,
    vocab: list[str] | None = None,
    replacements: list[tuple[str, str]] | None = None,
    settings: dict | None = None,
    device: str = "other-device",
    generated_at: str | None = None,
) -> None:
    """Write a newer-than-local sync.json to `sync_path`. `generated_at`
    defaults to "now" — use an explicit string to pin a specific
    relative time for staleness tests."""
    from datetime import datetime, timezone

    sync_block = {
        "vocabulary": [{"phrase": v, "added_at": "2026-04-21T00:00:00+00:00",
                         "notes": None} for v in (vocab or [])],
        "replacements": [
            {
                "from": f, "to": t, "category": "Replacement",
                "added_at": "2026-04-21T00:00:00+00:00",
            }
            for f, t in (replacements or [])
        ],
        "settings": settings or {},
    }
    bundle = {
        "schema_version": 1,
        "format": sync.FORMAT_TAG,
        "generated_at": generated_at
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_by_device": device,
        "local_sync_hash": sync.stable_hash(sync_block),
        "sync": sync_block,
        "local": {},
    }
    sync_path.write_text(json.dumps(bundle, indent=2))


def _force_old_mtime(p: Path) -> None:
    """Backdate a file's mtime to 10 minutes ago so a "now" sync stamp
    compares as newer. Time-skew-resistant; no sleeps needed. Skips
    when the file doesn't exist yet — some tests only have config.toml
    without a sidecar file."""
    if not p.exists():
        return
    past = time.time() - 600
    os.utime(p, (past, past))


# ---------------------------------------------------------------------------
# Conflict files
# ---------------------------------------------------------------------------

def test_find_conflict_files_returns_empty_when_none(paths) -> None:
    assert sync.find_sync_conflict_files(paths["sync"]) == []


def test_find_conflict_files_globs_sibling_conflicts(paths) -> None:
    paths["sync"].write_text("{}")
    c1 = paths["sync"].parent / "sync.sync-conflict-20260421-abc.json"
    c2 = paths["sync"].parent / "sync.sync-conflict-20260422-xyz.json"
    c1.write_text("{}")
    c2.write_text("{}")
    # Unrelated file stays out.
    (paths["sync"].parent / "other.json").write_text("{}")
    found = sync.find_sync_conflict_files(paths["sync"])
    assert set(found) == {c1, c2}


def test_reconcile_conflict_short_circuits(paths) -> None:
    _write_sync_bundle(paths["sync"], vocab=["ShouldNotApply"])
    conflict = paths["sync"].parent / "sync.sync-conflict-1.json"
    conflict.write_text("{}")

    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.Sidecar()

    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"],
        sidecar_path=paths["side"],
        sync_path=paths["sync"],
        models_dir=paths["models"],
    )

    assert result.conflict_files == [conflict]
    assert result.applied_from is None
    assert result.skipped_reason == "conflict"
    # No mutation despite a valid bundle being present.
    assert not any(v.phrase == "ShouldNotApply" for v in sc.vocabulary)


# ---------------------------------------------------------------------------
# Absence / corruption / schema
# ---------------------------------------------------------------------------

def test_reconcile_no_sync_file_is_silent(paths) -> None:
    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.Sidecar()
    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"], sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )
    assert result.skipped_reason == "no_file"
    assert result.warnings == []
    assert result.applied_from is None


def test_reconcile_corrupt_sync_is_ignored_with_warning(paths) -> None:
    paths["sync"].write_text("{ broken JSON ")
    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.Sidecar()
    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"], sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )
    assert result.skipped_reason == "corrupt"
    assert result.applied_from is None
    assert result.warnings, "expected a warning so the app shell can flag it"


def test_reconcile_schema_too_new_is_ignored(paths) -> None:
    paths["sync"].write_text(json.dumps({
        "schema_version": 99, "format": sync.FORMAT_TAG,
        "generated_at": "2999-01-01T00:00:00Z",
        "generated_by_device": "future-device",
        "local_sync_hash": "0" * 64,
        "sync": {"vocabulary": [], "replacements": [], "settings": {}},
        "local": {},
    }))
    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.Sidecar()
    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"], sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )
    assert result.applied_from is None
    assert result.skipped_reason == "corrupt"


def test_reconcile_unparseable_generated_at_is_ignored(paths) -> None:
    """A bundle with garbage `generated_at` (e.g. corrupted by a buggy
    peer) must not crash the reader."""
    bundle = {
        "schema_version": 1, "format": sync.FORMAT_TAG,
        "generated_at": "nonsense-timestamp",
        "generated_by_device": "quirky",
        "local_sync_hash": "0" * 64,
        "sync": {"vocabulary": [], "replacements": [], "settings": {}},
        "local": {},
    }
    paths["sync"].write_text(json.dumps(bundle))
    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.Sidecar()
    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"], sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )
    assert result.applied_from is None
    assert result.skipped_reason == "corrupt"


# ---------------------------------------------------------------------------
# Staleness compare
# ---------------------------------------------------------------------------

def test_reconcile_local_newer_is_noop(paths) -> None:
    """Sync bundle older than local config → no apply."""
    _write_sync_bundle(
        paths["sync"], vocab=["ShouldNotApply"],
        generated_at="2000-01-01T00:00:00Z",  # definitely stale
    )
    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.Sidecar()
    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"], sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )
    assert result.applied_from is None
    assert result.skipped_reason == "local_newer"
    assert not any(v.phrase == "ShouldNotApply" for v in sc.vocabulary)


def test_reconcile_sync_newer_applies_in_memory_only(paths) -> None:
    """Sync bundle stamp > local mtime → apply mutates `doc`/`sc`
    in-memory and flags the result as needing a save.

    Disk is NOT touched — that was the old `_persist_after_apply`
    behavior which silently overwrote user-edited settings on every
    launch ("model changes by itself"). The user must now Ctrl+S to
    persist the synced changes."""
    # Backdate config + metadata so the sync's "now" stamp wins.
    paths["side"].write_text('{"version":1,"vocabulary":[],"replacements":[]}')
    _force_old_mtime(paths["cfg"])
    _force_old_mtime(paths["side"])
    # Guard: the sync-reconcile model filter requires the incoming
    # model to be installed locally. Pre-provision it so this test
    # verifies the happy-path apply, not the filter's skip behavior.
    paths["models"].mkdir(exist_ok=True)
    (paths["models"] / "ggml-tiny.bin").write_bytes(b"x" * 100)

    _write_sync_bundle(
        paths["sync"],
        vocab=["FromOtherDevice"],
        replacements=[("slashdeploy", "/deploy")],
        settings={"whisper": {"model": "tiny"}},
        device="other-laptop",
    )
    pre_cfg_bytes = paths["cfg"].read_bytes()
    pre_side_bytes = paths["side"].read_bytes()

    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.load(paths["side"])

    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"], sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )

    assert result.applied_from == "other-laptop"
    assert result.skipped_reason is None
    # In-memory state reflects the apply.
    assert any(v.phrase == "FromOtherDevice" for v in sc.vocabulary)
    assert str(doc["whisper"]["model"]) == "tiny"
    # Dirty flags tell the caller (AppState.load) to set config_dirty.
    assert result.needs_save_doc is True
    assert result.needs_save_sidecar is True
    # Disk is untouched — persist is now the user's responsibility.
    assert paths["cfg"].read_bytes() == pre_cfg_bytes
    assert paths["side"].read_bytes() == pre_side_bytes
    # Settings changes are surfaced for the banner.
    assert any(
        "whisper.model" in c for c in result.applied_settings_changes
    )


def test_reconcile_identical_content_is_noop(paths) -> None:
    """Bundle stamp is newer but content matches local → don't loop-write."""
    _force_old_mtime(paths["cfg"])
    _force_old_mtime(paths["side"])

    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.Sidecar()
    # Bundle mirrors current state (vocab/reps/settings all empty of
    # meaningful additions). Distill current state, embed it.
    current = sync.distill_sync(doc, sc.vocabulary, sc.replacements)
    bundle = {
        "schema_version": 1, "format": sync.FORMAT_TAG,
        "generated_at": "2999-01-01T00:00:00Z",
        "generated_by_device": "twin",
        "local_sync_hash": sync.stable_hash(current),
        "sync": current,
        "local": {},
    }
    paths["sync"].write_text(json.dumps(bundle))

    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"], sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )
    assert result.applied_from is None
    assert result.skipped_reason == "identical"


def test_reconcile_subset_bundle_is_silent_noop(paths) -> None:
    """Bundle stamp is newer AND content hash differs, but the bundle's
    entries are a subset of local state — apply_bundle_to_state merges
    nothing new. The reader must NOT surface a "Synced from" banner
    (there's nothing new to announce) and must NOT persist (which would
    only bump config.toml's mtime for nothing).

    Repro of the real-world single-device false positive: an older
    sync.json with empty vocab predates the phrases the user added
    through the UI after that sync was written."""
    # Local has vocab entries; bundle is empty. Backdate the local files
    # so the stale sync.json still "wins" the mtime compare.
    cfg = paths["cfg"]
    cfg_doc = tomlkit.parse(cfg.read_text())
    if "whisper" not in cfg_doc:
        cfg_doc["whisper"] = tomlkit.table()
    cfg_doc["whisper"]["initial_prompt"] = "Alpha, Beta, Gamma"
    cfg.write_text(tomlkit.dumps(cfg_doc))
    paths["side"].write_text(json.dumps({
        "version": 1,
        "vocabulary": [
            {"phrase": "Alpha", "added_at": "2026-04-20T00:00:00+00:00"},
            {"phrase": "Beta", "added_at": "2026-04-20T00:00:00+00:00"},
            {"phrase": "Gamma", "added_at": "2026-04-20T00:00:00+00:00"},
        ],
        "replacements": [],
    }))
    _force_old_mtime(cfg)
    _force_old_mtime(paths["side"])

    # Empty bundle, but fresh timestamp → "newer than local". Device
    # label is this-machine-like to mirror the real scenario.
    _write_sync_bundle(paths["sync"], vocab=[], device="ai-node")

    doc = tomlkit.parse(cfg.read_text())
    sc = sidecar_mod.load(paths["side"])
    cfg_mtime_before = cfg.stat().st_mtime
    side_mtime_before = paths["side"].stat().st_mtime

    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=cfg, sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )

    assert result.applied_from is None
    assert result.skipped_reason == "noop_merge"
    # Local state preserved — merge semantics kept our phrases.
    assert [v.phrase for v in sc.vocabulary] == ["Alpha", "Beta", "Gamma"]
    # No persist: mtimes unchanged so next startup makes the same
    # decision without an extra disk churn cycle.
    assert cfg.stat().st_mtime == cfg_mtime_before
    assert paths["side"].stat().st_mtime == side_mtime_before


# ---------------------------------------------------------------------------
# Missing model
# ---------------------------------------------------------------------------

def test_reconcile_flags_missing_model(paths) -> None:
    paths["side"].write_text('{"version":1,"vocabulary":[],"replacements":[]}')
    _force_old_mtime(paths["cfg"])
    _force_old_mtime(paths["side"])

    # models_dir doesn't exist at all → any named model is missing.
    _write_sync_bundle(
        paths["sync"],
        settings={"whisper": {"model": "large-v3-turbo"}},
        device="other",
    )
    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.load(paths["side"])

    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"], sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )
    # Post-guard behavior: the filter strips whisper.model (not
    # installed), so there's nothing left to apply → noop_merge. But
    # the missing-model banner still fires so the user can download
    # it. This replaces the pre-guard behavior where we applied the
    # uninstalled name to disk and let the daemon crash on restart.
    assert result.missing_model == "large-v3-turbo"
    assert result.skipped_reason == "noop_merge"
    # Local whisper.model must NOT have been overwritten with the
    # uninstalled name — the whole point of the guard.
    assert "model" not in (doc.get("whisper") or {}) or (
        str(doc["whisper"]["model"]) != "large-v3-turbo"
    )


def test_reconcile_no_missing_model_when_present(paths) -> None:
    paths["side"].write_text('{"version":1,"vocabulary":[],"replacements":[]}')
    _force_old_mtime(paths["cfg"])
    _force_old_mtime(paths["side"])

    paths["models"].mkdir(exist_ok=True)
    (paths["models"] / "ggml-tiny.bin").write_bytes(b"fake model content")

    _write_sync_bundle(
        paths["sync"], settings={"whisper": {"model": "tiny"}}, device="dev",
    )
    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.load(paths["side"])

    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"], sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )
    assert result.applied_from == "dev"
    assert result.missing_model is None


# ---------------------------------------------------------------------------
# Pilot tests — banners
# ---------------------------------------------------------------------------

async def test_no_banners_when_no_sync_file(tmp_env) -> None:
    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        conflict = app.query_one(SyncConflictBanner)
        applied = app.query_one(AppliedSyncBanner)
        missing = app.query_one(MissingModelBanner)
        assert conflict.display is False
        assert applied.display is False
        assert missing.display is False


async def test_conflict_banner_surfaces(tmp_env) -> None:
    tmp_env["sync"].write_text("{}")
    conflict = tmp_env["sync"].parent / "sync.sync-conflict-20260421.json"
    conflict.write_text("{}")
    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one(SyncConflictBanner)
        assert banner.display is True
        assert banner.conflict_files == [conflict]


async def test_applied_banner_shows_after_apply(tmp_env) -> None:
    """Full path: bundle stamped in the future → startup applies →
    AppliedSyncBanner visible."""
    _force_old_mtime(tmp_env["cfg"])
    _force_old_mtime(tmp_env["side"]) if tmp_env["side"].exists() else None

    _write_sync_bundle(
        tmp_env["sync"], vocab=["SyncedFromAir"],
        device="zeus-laptop",
        generated_at="2999-06-01T00:00:00Z",
    )
    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        applied = app.query_one(AppliedSyncBanner)
        assert applied.display is True
        # Vocab merged into in-memory state AND persisted.
        assert any(v.phrase == "SyncedFromAir" for v in app.state.sc.vocabulary)


async def test_applied_banner_dismiss_hides(tmp_env) -> None:
    _force_old_mtime(tmp_env["cfg"])
    if tmp_env["side"].exists():
        _force_old_mtime(tmp_env["side"])
    _write_sync_bundle(
        tmp_env["sync"], vocab=["Alpha"],
        generated_at="2999-06-01T00:00:00Z",
    )
    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        applied = app.query_one(AppliedSyncBanner)
        assert applied.display is True
        await pilot.click("#dismiss-synced")
        await pilot.pause()
        assert applied.display is False


async def test_missing_model_banner_surfaces(tmp_env) -> None:
    _force_old_mtime(tmp_env["cfg"])
    if tmp_env["side"].exists():
        _force_old_mtime(tmp_env["side"])
    _write_sync_bundle(
        tmp_env["sync"],
        settings={"whisper": {"model": "large-v3-turbo"}},
        device="beefy-box",
        generated_at="2999-06-01T00:00:00Z",
    )
    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        missing = app.query_one(MissingModelBanner)
        assert missing.display is True
        assert missing.model_name == "large-v3-turbo"


async def test_reload_clears_applied_and_missing_banners(tmp_env) -> None:
    """Ctrl+R path: after a successful apply, if sync.json is gone the
    next reload should clear the applied + missing banners.

    Note: the apply now sets config_dirty (see Fix #2 — persist is
    opt-in via Ctrl+S). Ctrl+R refuses to discard unsaved changes, so
    the test saves first."""
    _force_old_mtime(tmp_env["cfg"])
    if tmp_env["side"].exists():
        _force_old_mtime(tmp_env["side"])
    _write_sync_bundle(
        tmp_env["sync"], vocab=["One"],
        generated_at="2999-06-01T00:00:00Z",
    )
    app = VoxtypeTUI(config_path=tmp_env["cfg"], sidecar_path=tmp_env["side"])
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(AppliedSyncBanner).display is True

        # Save the synced changes so the next Ctrl+R isn't blocked by
        # the unsaved-changes guard.
        await pilot.press("ctrl+s")
        await pilot.pause()

        # Remove the sync file so the next reload has nothing to apply
        # from. Ctrl+R.
        tmp_env["sync"].unlink()
        await pilot.press("ctrl+r")
        await pilot.pause()

        assert app.query_one(AppliedSyncBanner).display is False


# ---------------------------------------------------------------------------
# Same-device drift guard — the Fix #1 contract
# ---------------------------------------------------------------------------

def test_same_device_drift_keeps_local_settings(paths, monkeypatch) -> None:
    """Same-device sync.json with a model different from live config →
    the drift check strips settings from the bundle so local wins.

    This is the fix for "model changes by itself": user sets
    `whisper.model = "large-v3"` in the TUI, saves, later a stale bundle
    arrives (or was left over from an earlier session) still carrying
    `whisper.model = "base.en"`. Under the old mtime-only compare the
    bundle's settings would blindly overwrite local. Now the drift
    check detects "I wrote this bundle, but local no longer hashes to
    what the bundle carries" and protects the model field."""
    paths["side"].write_text('{"version":1,"vocabulary":[],"replacements":[]}')
    _force_old_mtime(paths["cfg"])
    _force_old_mtime(paths["side"])
    paths["models"].mkdir(exist_ok=True)
    (paths["models"] / "ggml-base.en.bin").write_bytes(b"x" * 100)
    (paths["models"] / "ggml-large-v3.bin").write_bytes(b"x" * 100)

    # Set local to large-v3 explicitly.
    cfg_doc = tomlkit.parse(paths["cfg"].read_text())
    if "whisper" not in cfg_doc:
        cfg_doc["whisper"] = tomlkit.table()
    cfg_doc["whisper"]["model"] = "large-v3"
    paths["cfg"].write_text(tomlkit.dumps(cfg_doc))
    _force_old_mtime(paths["cfg"])

    # Stale bundle written by THIS device with the old `base.en` value.
    monkeypatch.setattr(sync, "get_device_label", lambda: "zeus-machine")
    _write_sync_bundle(
        paths["sync"],
        vocab=["SharedVocab"],
        settings={"whisper": {"model": "base.en"}},
        device="zeus-machine",  # same device → drift check engages
    )

    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.load(paths["side"])
    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"], sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )

    # Local model survived — settings were stripped before apply.
    assert str(doc["whisper"]["model"]) == "large-v3"
    # Vocab still merged (additive, always safe).
    assert any(v.phrase == "SharedVocab" for v in sc.vocabulary)
    # Result surfaces the suppressed change for the banner.
    assert any(
        "whisper.model" in c for c in result.suppressed_settings_changes
    )


def test_cross_device_bundle_still_applies_settings(paths, monkeypatch) -> None:
    """Bundle from a DIFFERENT device (Syncthing cross-device case) must
    still apply settings — that's the whole point of sync. The drift
    check is scoped to same-device via `generated_by_device` vs local
    hostname."""
    paths["side"].write_text('{"version":1,"vocabulary":[],"replacements":[]}')
    _force_old_mtime(paths["cfg"])
    _force_old_mtime(paths["side"])
    paths["models"].mkdir(exist_ok=True)
    (paths["models"] / "ggml-tiny.bin").write_bytes(b"x" * 100)

    monkeypatch.setattr(sync, "get_device_label", lambda: "this-machine")
    _write_sync_bundle(
        paths["sync"],
        settings={"whisper": {"model": "tiny"}},
        device="other-laptop",  # different device → drift check skipped
    )

    doc = tomlkit.parse(paths["cfg"].read_text())
    sc = sidecar_mod.load(paths["side"])
    result = sync.reconcile_sync_on_startup(
        doc, sc,
        config_path=paths["cfg"], sidecar_path=paths["side"],
        sync_path=paths["sync"], models_dir=paths["models"],
    )
    assert str(doc["whisper"]["model"]) == "tiny"
    assert result.suppressed_settings_changes == []
    assert any(
        "whisper.model" in c for c in result.applied_settings_changes
    )
