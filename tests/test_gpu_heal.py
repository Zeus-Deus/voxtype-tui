"""Startup self-heal for pre-existing (v0.1.7-era) GPU drop-ins.

Wired into VoxtypeTUI.on_mount via `_heal_gpu_dropin` (voxtype_tui/app.py),
this rewrites a gpu.conf that only sets VOXTYPE_VULKAN_DEVICE (no
VK_LOADER_DRIVERS_SELECT) so existing multi-GPU installs get the loader
fix without the user re-toggling the Settings-tab Select. See
voxtype_tui/gpu.py module docstring for the underlying bug.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from voxtype_tui import gpu as gpu_mod
from voxtype_tui import voxtype_cli
from voxtype_tui.app import VoxtypeTUI
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


def _track_notify(app):
    notifications: list[tuple[str, str]] = []
    orig_notify = app.notify

    def track(message, *a, severity="information", **kw):
        notifications.append((severity, str(message)))
        return orig_notify(message, *a, severity=severity, **kw)

    app.notify = track  # type: ignore[method-assign]
    return notifications


async def test_heal_broken_dropin_writes_reloads_stales_notifies_once(
    tmp_env, tmp_path, monkeypatch,
):
    cfg, side = tmp_env
    dropin = tmp_path / "gpu.conf"
    # v0.1.7-format: only the VOXTYPE line, no loader line.
    dropin.write_text('[Service]\nEnvironment="VOXTYPE_VULKAN_DEVICE=nvidia"\n')

    write_calls: list[tuple[Path, str | None]] = []
    reload_calls: list[int] = []

    monkeypatch.setattr(gpu_mod, "DROPIN_PATH", dropin)
    monkeypatch.setattr(
        gpu_mod, "write_gpu_device",
        lambda path, vendor: write_calls.append((path, vendor)),
    )
    monkeypatch.setattr(
        gpu_mod, "daemon_reload",
        lambda *a, **kw: reload_calls.append(1) or (True, "ok"),
    )
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/" + n)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    notifications = _track_notify(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert write_calls == [(dropin, "nvidia")]
        assert reload_calls == [1]
        assert app.state.daemon_stale is True
        assert app.query_one("#daemon-stale").has_class("visible")

    heal_notes = [m for _, m in notifications if "GPU device config updated" in m]
    assert len(heal_notes) == 1
    assert "nvidia" in heal_notes[0]


async def test_heal_canonical_dropin_is_strict_noop(tmp_env, tmp_path, monkeypatch):
    cfg, side = tmp_env
    dropin = tmp_path / "gpu.conf"
    # Write via the real function first to get a genuinely canonical file.
    gpu_mod.write_gpu_device(dropin, "amd")

    write_calls: list[tuple[Path, str | None]] = []
    reload_calls: list[int] = []

    monkeypatch.setattr(gpu_mod, "DROPIN_PATH", dropin)
    monkeypatch.setattr(
        gpu_mod, "write_gpu_device",
        lambda path, vendor: write_calls.append((path, vendor)),
    )
    monkeypatch.setattr(
        gpu_mod, "daemon_reload",
        lambda *a, **kw: reload_calls.append(1) or (True, "ok"),
    )
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/" + n)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    notifications = _track_notify(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert write_calls == []
        assert reload_calls == []
        assert app.state.daemon_stale is False

    heal_notes = [m for _, m in notifications if "GPU device config updated" in m]
    assert heal_notes == []


async def test_heal_missing_dropin_is_strict_noop(tmp_env, tmp_path, monkeypatch):
    cfg, side = tmp_env
    dropin = tmp_path / "gpu.conf"  # never created

    write_calls: list[tuple[Path, str | None]] = []
    reload_calls: list[int] = []

    monkeypatch.setattr(gpu_mod, "DROPIN_PATH", dropin)
    monkeypatch.setattr(
        gpu_mod, "write_gpu_device",
        lambda path, vendor: write_calls.append((path, vendor)),
    )
    monkeypatch.setattr(
        gpu_mod, "daemon_reload",
        lambda *a, **kw: reload_calls.append(1) or (True, "ok"),
    )
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/" + n)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    notifications = _track_notify(app)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()

        assert write_calls == []
        assert reload_calls == []
        assert app.state.daemon_stale is False

    heal_notes = [m for _, m in notifications if "GPU device config updated" in m]
    assert heal_notes == []


async def test_heal_skips_daemon_reload_when_systemctl_absent(
    tmp_env, tmp_path, monkeypatch,
):
    """No systemctl on PATH -> heal still writes + notifies, but never
    calls daemon_reload."""
    cfg, side = tmp_env
    dropin = tmp_path / "gpu.conf"
    dropin.write_text('[Service]\nEnvironment="VOXTYPE_VULKAN_DEVICE=intel"\n')

    write_calls: list[tuple[Path, str | None]] = []
    reload_calls: list[int] = []

    monkeypatch.setattr(gpu_mod, "DROPIN_PATH", dropin)
    monkeypatch.setattr(
        gpu_mod, "write_gpu_device",
        lambda path, vendor: write_calls.append((path, vendor)),
    )
    monkeypatch.setattr(
        gpu_mod, "daemon_reload",
        lambda *a, **kw: reload_calls.append(1) or (True, "ok"),
    )
    monkeypatch.setattr(shutil, "which", lambda n: None)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()

        assert write_calls == [(dropin, "intel")]
        assert reload_calls == []
        assert app.state.daemon_stale is True
