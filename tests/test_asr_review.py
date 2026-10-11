"""Public save/restart regressions; all service I/O is synthetic here."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from voxtype_tui import asr_server as asr, config, extra_models as xm, sidecar
from voxtype_tui.state import AppState
from .conftest import FIXTURES


@pytest.fixture
def managed(tmp_path, monkeypatch):
    cfg = tmp_path / "primary" / "config.toml"
    meta = tmp_path / "primary" / "metadata.json"
    cfg.parent.mkdir()
    shutil.copy(FIXTURES / "stock.toml", cfg)
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.setattr(sidecar, "SIDECAR_PATH", meta)
    md = xm.default_models_dir()
    md.mkdir(parents=True)
    (md / "qwen3-asr-0.6b-q4_k.gguf").write_bytes(b"fixture model")
    (md / "ggml-base.en.bin").write_bytes(b"fixture native model")
    binary = xm.crispasr_binary("vulkan")
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    st = AppState.load(cfg, meta)
    asr.set_active_model(st, xm.ENGINE_QWEN3, "0.6b-q4_k", port=40123)
    calls = []
    monkeypatch.setattr(asr, "_systemctl", lambda *a, **k: (calls.append(a), (0, ""))[1])
    return st, calls


@pytest.mark.parametrize("alternate", ["config", "sidecar", "both"])
@pytest.mark.parametrize("active", [False, True])
def test_alternate_save_leaves_primary_helper_untouched(managed, tmp_path, alternate, active):
    primary, calls = managed
    primary.save()
    unit = asr.unit_path().read_bytes()
    env = asr.env_path().read_bytes()
    cfg = tmp_path / "alternate.toml" if alternate in ("config", "both") else primary.config_path
    meta = tmp_path / "alternate.json" if alternate in ("sidecar", "both") else primary.sidecar_path
    shutil.copy(primary.config_path, cfg) if cfg != primary.config_path else None
    shutil.copy(primary.sidecar_path, meta) if meta != primary.sidecar_path else None
    st = AppState.load(cfg, meta)
    if not active:
        asr.set_active_model(st, "whisper", "base.en")
    st.add_vocab("FixtureWord")
    calls.clear()
    st.save()
    assert asr.unit_path().read_bytes() == unit
    assert asr.env_path().read_bytes() == env
    assert calls == []
    assert st.asr_problems == []
    assert "FixtureWord" in cfg.read_text()


async def test_alternate_settings_gpu_leaves_primary_helper_untouched(managed, tmp_path, monkeypatch):
    from voxtype_tui import gpu, settings, voxtype_cli
    from voxtype_tui.app import VoxtypeTUI
    primary, calls = managed
    primary.save()
    unit = asr.unit_path().read_bytes()
    env = asr.env_path().read_bytes()
    cfg, meta = tmp_path / "alternate.toml", tmp_path / "alternate.json"
    shutil.copy(primary.config_path, cfg)
    shutil.copy(primary.sidecar_path, meta)
    monkeypatch.setattr(gpu, "DROPIN_PATH", tmp_path / "gpu.conf")
    monkeypatch.setattr(gpu, "daemon_reload", lambda: (True, "fixture"))
    monkeypatch.setattr(settings, "gpu_status_sync", lambda: (True, "fixture"))
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    async def inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", inactive)
    app = VoxtypeTUI(config_path=cfg, sidecar_path=meta)
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.pause()
        calls.clear()
        app.query_one(settings.SettingsPane)._apply_gpu_device("nvidia")
        await pilot.pause()
        assert asr.unit_path().read_bytes() == unit
        assert asr.env_path().read_bytes() == env
        assert calls == []


@pytest.mark.parametrize("entrypoint", ["sync", "async", "tui", "modal", "exit", "migrate"])
async def test_switch_save_preserves_helper_until_public_restart(managed, monkeypatch, entrypoint):
    from types import SimpleNamespace
    from voxtype_tui import cli_migrate, voxtype_cli
    from voxtype_tui import app as app_module
    st, calls = managed
    st.save()
    unit, env = asr.unit_path().read_bytes(), asr.env_path().read_bytes()
    asr.set_active_model(st, "whisper", "base.en")
    calls.clear()
    st.save()
    assert asr.unit_path().read_bytes() == unit
    assert asr.env_path().read_bytes() == env
    assert not any(a[0] in ("disable", "stop") for a in calls)
    assert st.daemon_stale
    # Pending teardown must survive closing/reopening the frontend.
    reopened = AppState.load(st.config_path, st.sidecar_path)
    assert reopened.daemon_stale
    identities = iter(["101\n1001\n", "102\n1002\n"])
    restart_calls = []
    def run(argv, **kwargs):
        if argv[:3] == ["systemctl", "--user", "show"]:
            return SimpleNamespace(returncode=0, stdout=next(identities), stderr="")
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if argv[:3] == ["systemctl", "--user", "restart"]:
            restart_calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(voxtype_cli.subprocess, "run", run)
    monkeypatch.setattr(voxtype_cli.shutil, "which", lambda _: "/fixture/bin")
    if entrypoint == "sync":
        assert voxtype_cli.restart_daemon()[0]
    elif entrypoint == "async":
        assert (await voxtype_cli.restart_daemon_async())[0]
    elif entrypoint == "exit":
        app_module._restart_daemon_on_exit_if_needed(SimpleNamespace(state=reopened))
    elif entrypoint == "migrate":
        reopened.sc.version = 1
        sidecar.save_atomic(reopened.sc, reopened.sidecar_path)
        assert cli_migrate.main(["--apply-migrations"]) == 0
    else:
        async def ready():
            return True
        monkeypatch.setattr(voxtype_cli, "wait_for_daemon_ready_async", ready)
        app = app_module.VoxtypeTUI(config_path=st.config_path, sidecar_path=st.sidecar_path)
        async with app.run_test() as pilot:
            if entrypoint == "modal":
                app.state.daemon_stale = False
                app.state.set_setting("hotkey.key", "F8")
                app.refresh_dirty()
                await pilot.press("ctrl+s")
                await pilot.pause()
                assert isinstance(app.screen, app_module.RestartModal)
                assert asr.unit_path().read_bytes() == unit
                await pilot.click("#yes")
            else:
                await pilot.press("ctrl+shift+r")
            for _ in range(20):
                await pilot.pause()
                if restart_calls and not app.restart_in_progress:
                    break
            assert not app.state.daemon_stale
    assert len(restart_calls) == 1
    assert ("disable", "--now", asr.UNIT_NAME) in calls
    assert not asr.unit_path().exists()
    assert not asr.env_path().exists()


def test_pending_helper_files_cannot_be_deleted(managed):
    st, _ = managed
    st.save()
    asr.set_active_model(st, "whisper", "base.en")
    st.save()
    with pytest.raises(xm.RefusedError, match="restart"):
        xm.delete(xm.ENGINE_QWEN3, "0.6b-q4_k", doc=st.doc, sc=st.sc)
    assert xm.is_downloaded(xm.ENGINE_QWEN3, "0.6b-q4_k")
    assert xm.build_installed("vulkan")


@pytest.mark.parametrize("failure", ["unchanged", "restart_failed", "timeout", "unknown_identity", "cleanup_failed"])
def test_failed_restart_retains_pending_teardown_for_retry(managed, monkeypatch, failure):
    import subprocess
    from types import SimpleNamespace
    from voxtype_tui import voxtype_cli
    st, calls = managed
    st.save()
    asr.set_active_model(st, "whisper", "base.en")
    st.save()
    identities = iter(["101\n1001\n", "101\n1001\n" if failure == "unchanged" else "102\n1002\n"])
    def run(argv, **kwargs):
        if argv[:3] == ["systemctl", "--user", "show"]:
            if failure == "unknown_identity":
                return SimpleNamespace(returncode=1, stdout="", stderr="fixture error")
            return SimpleNamespace(returncode=0, stdout=next(identities), stderr="")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 15)
        return SimpleNamespace(returncode=1 if failure == "restart_failed" else 0, stdout="", stderr="fixture failure" if failure == "restart_failed" else "")
    monkeypatch.setattr(voxtype_cli.subprocess, "run", run)
    monkeypatch.setattr(voxtype_cli.shutil, "which", lambda _: "/fixture/bin")
    if failure == "cleanup_failed":
        monkeypatch.setattr(asr, "_systemctl", lambda *a, **k: (1, "fixture cleanup failure"))
    assert voxtype_cli.restart_daemon()[0] is False
    assert asr.teardown_pending()
    assert asr.unit_path().exists() and asr.env_path().exists()
    # A subsequent verified restart owns the cleanup, not the failed attempt.
    identities = iter(["102\n1002\n", "103\n1003\n"])
    failure = "retry"
    monkeypatch.setattr(asr, "_systemctl", lambda *a, **k: (calls.append(a), (0, ""))[1])
    assert voxtype_cli.restart_daemon()[0] is True
    assert not asr.teardown_pending()
    assert not asr.unit_path().exists() and not asr.env_path().exists()


def test_reactivation_cancels_pending_teardown(managed):
    st, _ = managed
    st.save()
    asr.set_active_model(st, "whisper", "base.en")
    st.save()
    assert asr.teardown_pending()
    asr.set_active_model(st, xm.ENGINE_QWEN3, "0.6b-q4_k")
    st.save()
    assert not asr.teardown_pending()
    assert asr.unit_path().exists() and asr.env_path().exists()
