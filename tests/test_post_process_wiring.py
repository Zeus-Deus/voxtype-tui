"""Tests for auto-wiring `[output.post_process]` on save when sidecar has rules."""
from __future__ import annotations

from pathlib import Path

import tomlkit

from voxtype_tui import config, sidecar
from voxtype_tui.state import AppState


def _bootstrap(tmp_path: Path, config_body: str, sc: sidecar.Sidecar) -> AppState:
    cfg = tmp_path / "config.toml"
    cfg.write_text(config_body)
    scp = tmp_path / "metadata.json"
    sidecar.save_atomic(sc, scp)
    return AppState.load(cfg, scp)


def test_save_enables_post_process_when_rules_exist(tmp_path: Path) -> None:
    body = '[text]\nreplacements = {"slash codemux release" = "/codemux-release"}\n'
    sc = sidecar.Sidecar(replacements=[
        sidecar.ReplacementEntry(from_text="slash codemux release"),
    ])
    state = _bootstrap(tmp_path, body, sc)
    # Skip `voxtype -c` validation in the test environment.
    state.save = _patch_save_without_validation(state)
    state.save()
    doc = tomlkit.parse((tmp_path / "config.toml").read_text())
    pp = config.get_post_process(doc)
    assert pp.get("command") == config.POSTPROCESS_COMMAND
    assert pp.get("timeout_ms") == config.POSTPROCESS_TIMEOUT_MS


def test_save_skips_wiring_when_user_has_own_command(tmp_path: Path) -> None:
    body = (
        '[text]\nreplacements = {"slash codemux release" = "/codemux-release"}\n'
        '[output.post_process]\ncommand = "ollama-cleanup.sh"\ntimeout_ms = 30000\n'
    )
    sc = sidecar.Sidecar(replacements=[
        sidecar.ReplacementEntry(from_text="slash codemux release"),
    ])
    state = _bootstrap(tmp_path, body, sc)
    state.save = _patch_save_without_validation(state)
    state.save()
    doc = tomlkit.parse((tmp_path / "config.toml").read_text())
    # User's command must not be overwritten.
    assert config.get_post_process(doc).get("command") == "ollama-cleanup.sh"


def test_save_skips_wiring_when_no_rules(tmp_path: Path) -> None:
    body = "[whisper]\nmodel = \"small\"\n"
    sc = sidecar.Sidecar()
    state = _bootstrap(tmp_path, body, sc)
    state.save = _patch_save_without_validation(state)
    state.save()
    doc = tomlkit.parse((tmp_path / "config.toml").read_text())
    assert config.get_post_process(doc) == {}


def test_save_is_idempotent(tmp_path: Path) -> None:
    body = '[text]\nreplacements = {"slash codemux release" = "/codemux-release"}\n'
    sc = sidecar.Sidecar(replacements=[
        sidecar.ReplacementEntry(from_text="slash codemux release"),
    ])
    state = _bootstrap(tmp_path, body, sc)
    state.save = _patch_save_without_validation(state)
    state.save()
    before = (tmp_path / "config.toml").read_text()
    state.save()
    after = (tmp_path / "config.toml").read_text()
    assert before == after


# ---------------------------------------------------------------------------

def _patch_save_without_validation(state: AppState):
    """Return a bound `save` that skips voxtype-binary validation (the test
    machine may not match the binary version this user runs against)."""
    import asyncio
    import tomlkit
    from voxtype_tui import config as cfg, sidecar as sc_mod, sync

    def save() -> list[str]:
        state._ensure_post_process_enabled()
        baseline_doc = tomlkit.parse(state.loaded_dump)
        cfg.safe_save(state.doc, state.config_path, validate=False)
        sc_mod.save_atomic(state.sc, state.sidecar_path)
        try:
            sync.write_sync_bundle(state.doc, state.sc)
        except OSError:
            pass
        restart_fields = cfg.diff_restart_sensitive(baseline_doc, state.doc)
        if restart_fields:
            state.daemon_stale = True
        state.loaded_dump = tomlkit.dumps(state.doc)
        state.config_dirty = False
        state.sidecar_dirty = False
        return restart_fields

    return save
