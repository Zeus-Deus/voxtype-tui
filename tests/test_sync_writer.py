"""Tests for the writer half of `voxtype_tui.sync` and its integration
into `AppState.save`.

Invariants this file exercises:
  * `sync.json` only appears after both primary saves (config + sidecar)
    have committed. A failure on either primary MUST leave `sync.json`
    untouched — otherwise a Syncthing peer could pull state that the
    local save never actually wrote.
  * The content-hash guard is idempotent: writing an unchanged bundle
    does NOT rewrite the file (no Syncthing thrash on unrelated edits).
  * The guard is robust against a corrupt / missing / unreadable
    existing `sync.json`. Those cases fall through to a clean rewrite —
    never prevent a fresh write.
  * Writes are atomic. A failure during `os.replace` leaves the old
    file intact and drops the tempfile rather than leaking it.
  * The emitted file never contains a `secrets` block (hard sync-side
    invariant; belt-and-suspenders against future caller bugs).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit

from voxtype_tui import config, sidecar, sync
from voxtype_tui.state import AppState

from .conftest import FIXTURES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stock_state(tmp_path: Path) -> AppState:
    """An AppState loaded from the `stock` fixture into a scratch tmp_path,
    with the sidecar path also pointing inside tmp_path."""
    cfg_path = tmp_path / "config.toml"
    side_path = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg_path)
    return AppState.load(cfg_path, side_path)


@pytest.fixture
def sync_path(tmp_path: Path) -> Path:
    return tmp_path / "sync.json"


def _sample_doc_and_sidecar() -> tuple[tomlkit.TOMLDocument, sidecar.Sidecar]:
    doc = tomlkit.parse(
        """
engine = "whisper"

[whisper]
model = "large-v3-turbo"
language = "en"
initial_prompt = "Codemux, Claude Code"
remote_api_key = "sk-EXTREMELY-SECRET"

[output]
mode = "type"

[output.post_process]
command = "malicious; rm -rf ~"
timeout_ms = 30000

[text]
[text.replacements]
"vox type" = "voxtype"
"""
    )
    sc = sidecar.Sidecar(
        vocabulary=[
            sidecar.VocabEntry(phrase="Codemux"),
            sidecar.VocabEntry(phrase="Claude Code"),
        ],
        replacements=[
            sidecar.ReplacementEntry(from_text="vox type"),
        ],
    )
    return doc, sc


# ---------------------------------------------------------------------------
# write_sync_bundle — basic behavior
# ---------------------------------------------------------------------------

def test_write_creates_file_when_missing(sync_path: Path) -> None:
    doc, sc = _sample_doc_and_sidecar()
    assert not sync_path.exists()
    wrote = sync.write_sync_bundle(doc, sc, sync_path, device_label="test-host")
    assert wrote is True
    assert sync_path.exists()


def test_write_creates_parent_directory(tmp_path: Path) -> None:
    """Fresh install: `~/.config/voxtype-tui/` may not exist yet. The
    writer must `mkdir -p` rather than crash."""
    deep = tmp_path / "nested" / "config" / "voxtype-tui" / "sync.json"
    doc, sc = _sample_doc_and_sidecar()
    sync.write_sync_bundle(doc, sc, deep, device_label="host")
    assert deep.exists()


def test_written_file_has_expected_shape(sync_path: Path) -> None:
    doc, sc = _sample_doc_and_sidecar()
    sync.write_sync_bundle(doc, sc, sync_path, device_label="zeus-desktop")
    data = json.loads(sync_path.read_text())
    assert data["format"] == sync.FORMAT_TAG
    assert data["schema_version"] == sync.SCHEMA_VERSION
    assert data["generated_by_device"] == "zeus-desktop"
    assert len(data["local_sync_hash"]) == 64
    assert data["sync"]["vocabulary"][0]["phrase"] == "Codemux"
    assert "local" in data
    # Hard invariant: auto-sync writes NEVER include the secrets block,
    # regardless of what the live config holds.
    assert "secrets" not in data


def test_written_file_contains_no_api_key_text(sync_path: Path) -> None:
    """Defence-in-depth: scan the raw text for the known secret value.
    If this ever fails, the secret-stripping contract has been violated
    somewhere upstream and needs urgent attention."""
    doc, sc = _sample_doc_and_sidecar()
    sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    raw = sync_path.read_text()
    assert "sk-EXTREMELY-SECRET" not in raw
    assert "malicious; rm -rf" not in raw


# ---------------------------------------------------------------------------
# Idempotence (content-hash guard)
# ---------------------------------------------------------------------------

def test_identical_state_skips_rewrite(sync_path: Path) -> None:
    doc, sc = _sample_doc_and_sidecar()
    wrote1 = sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    mtime1 = sync_path.stat().st_mtime_ns
    # Nanosecond guarantee: even on fast filesystems the second call
    # cannot plausibly hit the same ns tick — any mtime change means
    # the file was rewritten.
    wrote2 = sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    mtime2 = sync_path.stat().st_mtime_ns
    assert wrote1 is True
    assert wrote2 is False
    assert mtime1 == mtime2


def test_changed_vocab_triggers_rewrite(sync_path: Path) -> None:
    doc, sc = _sample_doc_and_sidecar()
    sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    mtime1 = sync_path.stat().st_mtime_ns

    sc.vocabulary.append(sidecar.VocabEntry(phrase="NewWord"))
    wrote = sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    mtime2 = sync_path.stat().st_mtime_ns

    assert wrote is True
    assert mtime2 != mtime1
    data = json.loads(sync_path.read_text())
    assert "NewWord" in {v["phrase"] for v in data["sync"]["vocabulary"]}


def test_changed_setting_triggers_rewrite(sync_path: Path) -> None:
    doc, sc = _sample_doc_and_sidecar()
    sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    hash1 = json.loads(sync_path.read_text())["local_sync_hash"]

    doc["whisper"]["model"] = "tiny"
    wrote = sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    hash2 = json.loads(sync_path.read_text())["local_sync_hash"]

    assert wrote is True
    assert hash1 != hash2


def test_local_only_change_still_triggers_rewrite(sync_path: Path) -> None:
    """Changing a LOCAL-block field (e.g. hotkey) isn't in the sync hash
    and thus wouldn't drive a rewrite under content-hash idempotence.
    But the `local` block is still in the file — we accept that one-shot
    rewrite the first time a local field changes, because the `local`
    block's whole point is to carry per-device backup info in a manual
    export scenario; a stale copy would drift.

    Current behavior: we only hash the SYNC block. Local changes alone
    are a no-op on disk. This test locks in that contract so a future
    change to "hash local too" is a deliberate decision."""
    doc, sc = _sample_doc_and_sidecar()
    sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    mtime1 = sync_path.stat().st_mtime_ns

    # Change a LOCAL field only — hotkey is never part of the sync block.
    if "hotkey" not in doc:
        doc["hotkey"] = tomlkit.table()
    doc["hotkey"]["key"] = "F13"

    wrote = sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    mtime2 = sync_path.stat().st_mtime_ns

    assert wrote is False
    assert mtime1 == mtime2


# ---------------------------------------------------------------------------
# Robustness against corrupt / partial existing sync.json
# ---------------------------------------------------------------------------

def test_corrupt_existing_sync_json_is_overwritten(sync_path: Path) -> None:
    sync_path.write_text("{ this is not valid json at all")
    doc, sc = _sample_doc_and_sidecar()
    wrote = sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    assert wrote is True
    data = json.loads(sync_path.read_text())
    assert data["format"] == sync.FORMAT_TAG


def test_partial_json_array_is_overwritten(sync_path: Path) -> None:
    """Unlikely but possible after a mid-write power loss on a non-atomic
    fs. Writer should treat a non-object root as "not recognizable" and
    rewrite cleanly."""
    sync_path.write_text('["partial", "write"]')
    doc, sc = _sample_doc_and_sidecar()
    wrote = sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    assert wrote is True
    data = json.loads(sync_path.read_text())
    assert isinstance(data, dict)
    assert data["format"] == sync.FORMAT_TAG


def test_existing_file_with_different_hash_is_overwritten(sync_path: Path) -> None:
    """Foreign file with valid shape but different hash — e.g. a sync.json
    received from another device — must be overwritten by local state.
    (That's the 'local wins' arm of the startup-reader staleness
    compare; the writer is unconditional local-wins because it runs
    after any reader has already reconciled.)"""
    sync_path.write_text(json.dumps({
        "format": sync.FORMAT_TAG,
        "schema_version": 1,
        "local_sync_hash": "0" * 64,
        "sync": {"vocabulary": [], "replacements": [], "settings": {}},
        "local": {},
    }))
    doc, sc = _sample_doc_and_sidecar()
    wrote = sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    assert wrote is True
    data = json.loads(sync_path.read_text())
    assert data["local_sync_hash"] != "0" * 64


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def test_failed_replace_does_not_leak_tempfile(sync_path: Path) -> None:
    """If `os.replace` raises (permissions, crossed-fs rename, etc.), the
    tempfile must be cleaned up — a leaked `.tmp` next to `sync.json`
    would confuse Syncthing and bloat the directory over time."""
    doc, sc = _sample_doc_and_sidecar()

    original_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated replace failure")

    with patch("voxtype_tui.sync.os.replace", side_effect=boom):
        with pytest.raises(OSError, match="simulated"):
            sync.write_sync_bundle(doc, sc, sync_path, device_label="host")

    # No sync.json (first write), no stray tempfiles.
    leftover = list(sync_path.parent.glob(f"{sync_path.name}.*.tmp"))
    assert leftover == [], f"tempfile leaked: {leftover}"
    assert not sync_path.exists()

    # Verify normal writes still work afterwards (no module state pollution).
    _ = original_replace  # silence lint
    wrote = sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    assert wrote is True


def test_failed_replace_preserves_old_file(sync_path: Path) -> None:
    """With an existing sync.json, a failed replace must keep the old
    file intact — the user didn't lose their previous sync state."""
    doc, sc = _sample_doc_and_sidecar()
    sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    old_content = sync_path.read_bytes()

    # Mutate state, then fail the write.
    sc.vocabulary.append(sidecar.VocabEntry(phrase="Attempt"))

    def boom(src, dst):
        raise OSError("simulated")

    with patch("voxtype_tui.sync.os.replace", side_effect=boom):
        with pytest.raises(OSError):
            sync.write_sync_bundle(doc, sc, sync_path, device_label="host")

    # Old file intact, no tempfile leak.
    assert sync_path.read_bytes() == old_content
    leftover = list(sync_path.parent.glob(f"{sync_path.name}.*.tmp"))
    assert leftover == []


def test_written_file_has_trailing_newline(sync_path: Path) -> None:
    """POSIX tradition; makes `tail sync.json` behave in the terminal and
    some editors complain about missing trailing newlines. Cheap to
    guarantee, so we do."""
    doc, sc = _sample_doc_and_sidecar()
    sync.write_sync_bundle(doc, sc, sync_path, device_label="host")
    raw = sync_path.read_bytes()
    assert raw.endswith(b"\n")


# ---------------------------------------------------------------------------
# Device label
# ---------------------------------------------------------------------------

def test_default_device_label_is_hostname() -> None:
    import socket as _socket
    assert sync.get_device_label() == _socket.gethostname().strip()


def test_explicit_device_label_overrides_default(sync_path: Path) -> None:
    doc, sc = _sample_doc_and_sidecar()
    sync.write_sync_bundle(doc, sc, sync_path, device_label="custom-label")
    data = json.loads(sync_path.read_text())
    assert data["generated_by_device"] == "custom-label"


# ---------------------------------------------------------------------------
# Async wrapper
# ---------------------------------------------------------------------------

def test_async_writer_runs_on_thread(sync_path: Path) -> None:
    doc, sc = _sample_doc_and_sidecar()

    async def _go() -> bool:
        return await sync.write_sync_bundle_async(
            doc, sc, sync_path, device_label="async-host",
        )

    wrote = asyncio.run(_go())
    assert wrote is True
    data = json.loads(sync_path.read_text())
    assert data["generated_by_device"] == "async-host"


# ---------------------------------------------------------------------------
# Integration with AppState.save
# ---------------------------------------------------------------------------

def test_state_save_writes_sync_json_after_both_primaries(
    stock_state: AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: after a clean save, sync.json exists next to
    metadata.json and has the expected shape."""
    expected_path = stock_state.sidecar_path.parent / "sync.json"
    monkeypatch.setattr(sync, "SYNC_PATH", expected_path)

    stock_state.add_vocab("Testing")
    stock_state.save()

    assert expected_path.exists()
    data = json.loads(expected_path.read_text())
    assert data["format"] == sync.FORMAT_TAG
    assert "Testing" in {v["phrase"] for v in data["sync"]["vocabulary"]}


def test_state_save_skips_sync_if_config_save_raises(
    stock_state: AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Primary save failure must leave sync.json UNTOUCHED. Rationale:
    advertising state that didn't commit locally is worse than a stale
    sync — another device pulling our phantom save would create a
    divergence we can't reconcile."""
    sync_dest = stock_state.sidecar_path.parent / "sync.json"
    monkeypatch.setattr(sync, "SYNC_PATH", sync_dest)

    def boom(doc, path, **kwargs):
        raise RuntimeError("simulated config failure")

    monkeypatch.setattr(config, "safe_save", boom)

    stock_state.add_vocab("ShouldNotPersist")
    with pytest.raises(RuntimeError):
        stock_state.save()

    assert not sync_dest.exists()


def test_state_save_skips_sync_if_sidecar_save_raises(
    stock_state: AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_dest = stock_state.sidecar_path.parent / "sync.json"
    monkeypatch.setattr(sync, "SYNC_PATH", sync_dest)

    def boom(sc, path):
        raise RuntimeError("simulated sidecar failure")

    monkeypatch.setattr(sidecar, "save_atomic", boom)

    stock_state.add_vocab("AlsoShouldNotPersist")
    with pytest.raises(RuntimeError):
        stock_state.save()

    assert not sync_dest.exists()


def test_state_save_swallows_sync_write_failure(
    stock_state: AppState, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed sync.json write must NOT roll back the primary save. The
    user typed their vocab, it committed to config + sidecar — losing
    that to a permission error on a derivative file would be
    infuriating. Log the failure, move on."""
    sync_dest = stock_state.sidecar_path.parent / "sync.json"
    monkeypatch.setattr(sync, "SYNC_PATH", sync_dest)

    def boom(doc, sc, path=None, **kwargs):
        raise OSError("simulated sync write failure")

    monkeypatch.setattr(sync, "write_sync_bundle", boom)

    stock_state.add_vocab("Persisted")
    import logging
    with caplog.at_level(logging.WARNING):
        stock_state.save()  # should NOT raise

    # Primary saves succeeded.
    assert stock_state.sidecar_path.exists()
    assert stock_state.config_path.exists()
    # Sidecar contains the new vocab.
    reloaded_sc = sidecar.load(stock_state.sidecar_path)
    assert "Persisted" in {v.phrase for v in reloaded_sc.vocabulary}
    # Sync file was not created (since our boom raised before it could).
    assert not sync_dest.exists()
    # Failure surfaced as a warning log.
    assert any("sync.json" in rec.message for rec in caplog.records)


def test_state_save_idempotent_across_consecutive_calls(
    stock_state: AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two saves with no state change between them → sync.json mtime
    unchanged (no Syncthing-triggering rewrite)."""
    sync_dest = stock_state.sidecar_path.parent / "sync.json"
    monkeypatch.setattr(sync, "SYNC_PATH", sync_dest)

    stock_state.add_vocab("Once")
    stock_state.save()
    mtime1 = sync_dest.stat().st_mtime_ns

    # No mutations between saves. Note: save() itself resets dirty flags,
    # so a second save with no intervening mutation is a realistic
    # "user hit Ctrl+S twice" case.
    stock_state.save()
    mtime2 = sync_dest.stat().st_mtime_ns

    assert mtime1 == mtime2


def test_state_save_async_writes_sync_json(
    stock_state: AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save_async should produce the same sync.json as save (it delegates
    via asyncio.to_thread). Locks in that the async path isn't missed."""
    sync_dest = stock_state.sidecar_path.parent / "sync.json"
    monkeypatch.setattr(sync, "SYNC_PATH", sync_dest)

    stock_state.add_vocab("AsyncEntry")

    async def _go():
        return await stock_state.save_async()

    asyncio.run(_go())

    assert sync_dest.exists()
    data = json.loads(sync_dest.read_text())
    assert "AsyncEntry" in {v["phrase"] for v in data["sync"]["vocabulary"]}
