"""GPU section of the Settings pane — status display + Enable/Disable buttons
that pop a floating terminal for sudo."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from textual.widgets import Button, RichLog

from voxtype_tui import voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.settings import (
    SettingsPane,
    TERMINAL_LAUNCHER,
    gpu_status_sync,
    launch_gpu_command_in_terminal,
)
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


async def _goto_settings(pilot, app):
    await pilot.press("3")
    await pilot.pause()


# ---- unit tests for the helpers ----

def test_gpu_status_handles_missing_voxtype(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda n: None if n == "voxtype" else "/usr/bin/" + n)
    ok, text = gpu_status_sync()
    assert not ok
    assert "not found" in text


def test_gpu_status_happy_path(monkeypatch) -> None:
    class R:
        returncode = 0
        stdout = (
            "=== Voxtype Backend Status ===\n\n"
            "Active backend: CPU (AVX-512)\n"
            "GPUs detected:\n  1. NVIDIA RTX 3090\n"
        )
        stderr = ""
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/voxtype" if n == "voxtype" else None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: R())
    ok, text = gpu_status_sync()
    assert ok
    assert "Active backend" in text
    assert "RTX 3090" in text


def test_launch_gpu_without_helper_returns_instructions(monkeypatch) -> None:
    """When the omarchy helper isn't available, we don't crash — we return a
    message the user can copy-paste into a terminal."""
    monkeypatch.setattr(shutil, "which", lambda n: None if n == TERMINAL_LAUNCHER else "/usr/bin/" + n)
    ok, msg = launch_gpu_command_in_terminal("enable")
    assert not ok
    assert "sudo voxtype setup gpu --enable" in msg


def test_launch_gpu_invalid_action() -> None:
    ok, msg = launch_gpu_command_in_terminal("delete")  # type: ignore[arg-type]
    assert not ok
    assert "invalid action" in msg


def test_launch_gpu_spawns_terminal(monkeypatch) -> None:
    """When the helper is available, we call it with the correct command."""
    calls: list[list[str]] = []

    class FakeProc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)

    monkeypatch.setattr(shutil, "which", lambda n: "/bin/" + n)  # everything present
    monkeypatch.setattr(subprocess, "Popen", FakeProc)

    ok, msg = launch_gpu_command_in_terminal("enable")
    assert ok
    assert calls == [[TERMINAL_LAUNCHER, "sudo voxtype setup gpu --enable"]]

    calls.clear()
    ok, msg = launch_gpu_command_in_terminal("disable")
    assert ok
    assert calls == [[TERMINAL_LAUNCHER, "sudo voxtype setup gpu --disable"]]


# ---- UI tests ----

async def test_gpu_section_renders(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)
        # Buttons + log exist
        assert pane.query_one("#settings-gpu-refresh", Button) is not None
        assert pane.query_one("#settings-gpu-enable", Button) is not None
        assert pane.query_one("#settings-gpu-disable", Button) is not None
        assert pane.query_one("#settings-gpu-log", RichLog) is not None


class _FakeRun:
    """Stand-in for subprocess.run — returns a zero-exit result with empty
    output so the on-mount gpu_status_sync doesn't try to use the real
    system."""
    returncode = 0
    stdout = ""
    stderr = ""


async def test_enable_button_pops_terminal(tmp_env, monkeypatch):
    """Clicking Enable calls the terminal launcher — not the actual sudo
    command — and the TUI does not block."""
    cfg, side = tmp_env
    calls: list[list[str]] = []

    class FakeProc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)

    monkeypatch.setattr(shutil, "which", lambda n: "/bin/" + n)
    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeRun())

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        # The GPU section is below the scroll fold; post the Pressed event
        # directly rather than trying to click an offscreen widget.
        btn = app.query_one("#settings-gpu-enable", Button)
        btn.post_message(Button.Pressed(btn))
        await pilot.pause()

    assert any("--enable" in c[-1] for c in calls)


async def test_gpu_enable_does_not_dirty_state(tmp_env, monkeypatch):
    """The GPU section doesn't edit config.toml — its changes go through
    voxtype's own tooling. Clicking Enable/Disable should not flip
    config_dirty."""
    cfg, side = tmp_env
    monkeypatch.setattr(shutil, "which", lambda n: "/bin/" + n)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeRun())

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        btn = app.query_one("#settings-gpu-enable", Button)
        btn.post_message(Button.Pressed(btn))
        await pilot.pause()
        assert app.state.config_dirty is False
