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
    gpu_status_sync,
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


async def test_enable_button_pushes_password_modal(tmp_env, monkeypatch):
    """Clicking Enable now pushes an in-app SudoPasswordModal instead of
    shelling out to a terminal. No subprocess runs until the user submits
    the password."""
    from voxtype_tui.sudo import SudoPasswordModal

    cfg, side = tmp_env
    popen_calls: list[list[str]] = []
    run_calls: list[list[str]] = []

    class FakeProc:
        def __init__(self, cmd, **kw):
            popen_calls.append(cmd)

    def _fake_run(cmd, *a, **kw):
        # Anything the GPU button triggers that ends up here is a bug — the
        # only subprocess.run call should be the initial gpu --status on
        # mount. We tag that by returning _FakeRun(); anything else gets
        # recorded so the assertion below can flag it.
        if cmd and isinstance(cmd, list) and "sudo" in cmd:
            run_calls.append(cmd)
        return _FakeRun()

    monkeypatch.setattr(shutil, "which", lambda n: "/bin/" + n)
    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    monkeypatch.setattr(subprocess, "run", _fake_run)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        btn = app.query_one("#settings-gpu-enable", Button)
        btn.post_message(Button.Pressed(btn))
        await pilot.pause()

        # A SudoPasswordModal should now be on the screen stack.
        assert isinstance(app.screen, SudoPasswordModal), (
            f"expected SudoPasswordModal on top of stack, got {type(app.screen).__name__}"
        )
        # And we did NOT pop a terminal or invoke sudo yet.
        assert popen_calls == []
        assert run_calls == []


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


async def test_password_submit_runs_sudo_with_stdin(tmp_env, monkeypatch):
    """Submitting a password in the modal invokes sudo -S with the password
    on stdin — NEVER in argv — and passes the expected gpu command."""
    from voxtype_tui import sudo as sudo_mod
    from voxtype_tui.sudo import SudoPasswordModal, SudoResult

    cfg, side = tmp_env
    captured: dict[str, object] = {}

    def _fake_run_sudo(argv, password, timeout=30.0):
        captured["argv"] = list(argv)
        captured["password"] = password
        return SudoResult(ok=True, returncode=0, output="GPU enabled.", incorrect_password=False)

    monkeypatch.setattr(shutil, "which", lambda n: "/bin/" + n)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeRun())
    # Patch the reference used by settings.py, not the module-level one —
    # `from .sudo import run_sudo_command` binds a name at import time.
    from voxtype_tui import settings as settings_mod
    monkeypatch.setattr(settings_mod, "run_sudo_command", _fake_run_sudo)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        btn = app.query_one("#settings-gpu-enable", Button)
        btn.post_message(Button.Pressed(btn))
        await pilot.pause()

        assert isinstance(app.screen, SudoPasswordModal)
        pwd_input = app.screen.query_one("#sudo-password")
        pwd_input.value = "hunter2"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()  # let the async sudo task run

    assert captured["argv"] == ["voxtype", "setup", "gpu", "--enable"]
    assert captured["password"] == "hunter2"


async def test_password_cancel_does_not_run_sudo(tmp_env, monkeypatch):
    """Escaping out of the modal dismisses it with None — no sudo invocation."""
    from voxtype_tui.sudo import SudoPasswordModal

    cfg, side = tmp_env
    captured: dict[str, object] = {"called": False}

    def _fake_run_sudo(*a, **kw):
        captured["called"] = True
        raise AssertionError("run_sudo_command should NOT be called on cancel")

    monkeypatch.setattr(shutil, "which", lambda n: "/bin/" + n)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeRun())
    from voxtype_tui import settings as settings_mod
    monkeypatch.setattr(settings_mod, "run_sudo_command", _fake_run_sudo)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        btn = app.query_one("#settings-gpu-enable", Button)
        btn.post_message(Button.Pressed(btn))
        await pilot.pause()
        assert isinstance(app.screen, SudoPasswordModal)

        await pilot.press("escape")
        await pilot.pause()

    assert captured["called"] is False


async def test_wrong_password_reprompts(tmp_env, monkeypatch):
    """A SudoResult with incorrect_password=True should push a fresh
    SudoPasswordModal so the user can retry without re-clicking Enable."""
    from voxtype_tui.sudo import SudoPasswordModal, SudoResult

    cfg, side = tmp_env
    call_count = {"n": 0}

    def _fake_run_sudo(argv, password, timeout=30.0):
        call_count["n"] += 1
        return SudoResult(
            ok=False, returncode=1,
            output="", incorrect_password=True,
        )

    monkeypatch.setattr(shutil, "which", lambda n: "/bin/" + n)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeRun())
    from voxtype_tui import settings as settings_mod
    monkeypatch.setattr(settings_mod, "run_sudo_command", _fake_run_sudo)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        btn = app.query_one("#settings-gpu-enable", Button)
        btn.post_message(Button.Pressed(btn))
        await pilot.pause()

        assert isinstance(app.screen, SudoPasswordModal)
        app.screen.query_one("#sudo-password").value = "wrong"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        # After the bad sudo run, a fresh modal should be on top for the retry.
        assert isinstance(app.screen, SudoPasswordModal)
        assert call_count["n"] == 1
