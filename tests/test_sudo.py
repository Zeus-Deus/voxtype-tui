"""Unit tests for the in-app sudo helper.

Covers the modal's compose + dismiss paths and `run_sudo_command`'s argv
shape, stdin handling, and sudo-noise filtering.
"""
from __future__ import annotations

import subprocess

import pytest

from voxtype_tui import sudo as sudo_mod
from voxtype_tui.sudo import (
    SudoPasswordModal,
    SudoResult,
    _filter_sudo_noise,
    run_sudo_command,
)


class _FakeCompleted:
    def __init__(self, rc: int, stdout: str = "", stderr: str = ""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


# ---- run_sudo_command ----

def test_run_sudo_uses_S_flag_and_pipes_password_via_stdin(monkeypatch):
    """The password MUST arrive on stdin, never in argv. argv must be
    prefixed with `sudo -S -p '' --` so `--` separates our target from
    sudo's own flags (defense against argv poisoning of the target cmd)."""
    seen: dict = {}

    def _fake_run(cmd, input=None, **kw):
        seen["cmd"] = list(cmd)
        seen["input"] = input
        seen["kw"] = kw
        return _FakeCompleted(0, stdout="OK\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = run_sudo_command(["voxtype", "setup", "gpu", "--enable"], "s3cret")

    assert result.ok is True
    assert result.returncode == 0
    assert result.output == "OK"

    # argv shape: first four tokens are our safety harness, then --, then target
    assert seen["cmd"][:4] == ["sudo", "-S", "-p", ""]
    assert seen["cmd"][4] == "--"
    assert seen["cmd"][5:] == ["voxtype", "setup", "gpu", "--enable"]

    # Password ends in newline so sudo sees a full line; never in argv.
    assert seen["input"] == "s3cret\n"
    assert "s3cret" not in " ".join(seen["cmd"])


def test_run_sudo_detects_incorrect_password(monkeypatch):
    def _fake_run(cmd, input=None, **kw):
        return _FakeCompleted(1, stdout="", stderr="Sorry, try again.\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = run_sudo_command(["voxtype", "setup", "gpu", "--enable"], "wrong")

    assert result.ok is False
    assert result.returncode == 1
    assert result.incorrect_password is True


def test_run_sudo_handles_timeout(monkeypatch):
    def _fake_run(cmd, input=None, timeout=None, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = run_sudo_command(["voxtype", "setup", "gpu", "--enable"], "pw", timeout=1.0)

    assert result.ok is False
    assert "timed out" in result.output


def test_run_sudo_handles_missing_binary(monkeypatch):
    def _fake_run(cmd, input=None, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = run_sudo_command(["voxtype", "setup", "gpu", "--enable"], "pw")

    assert result.ok is False
    assert "sudo" in result.output


def test_run_sudo_rejects_empty_argv():
    result = run_sudo_command([], "pw")
    assert result.ok is False
    assert "empty" in result.output.lower()


def test_filter_sudo_noise_drops_prompt_lines():
    raw = (
        "[sudo] password for zeus: \n"
        "Actual output line\n"
        "Sorry, try again.\n"
        "1 incorrect password attempt\n"
        "Another output line\n"
    )
    filtered = _filter_sudo_noise(raw)
    assert "Actual output line" in filtered
    assert "Another output line" in filtered
    assert "[sudo]" not in filtered
    assert "Sorry, try again" not in filtered
    assert "incorrect password attempt" not in filtered


# ---- SudoPasswordModal ----

async def _with_modal(pilot_body):
    """Helper — push a SudoPasswordModal onto a blank app, run the async
    body, return the dismiss value."""
    from textual.app import App

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(
                SudoPasswordModal(
                    action_label="test action",
                    title="Test",
                ),
                self._capture,
            )

        def _capture(self, value):
            self._captured = value
            self.exit()

    host = _Host()
    async with host.run_test() as pilot:
        await pilot_body(pilot, host)
        await pilot.pause()
    return getattr(host, "_captured", "<no-dismiss>")


async def test_modal_submit_returns_password():
    async def _body(pilot, app):
        await pilot.pause()
        app.screen.query_one("#sudo-password").value = "p455"
        await pilot.press("enter")

    assert await _with_modal(_body) == "p455"


async def test_modal_cancel_returns_none():
    async def _body(pilot, app):
        await pilot.pause()
        await pilot.press("escape")

    assert await _with_modal(_body) is None


async def test_modal_empty_submit_is_noop():
    """Hitting Enter with no password entered shouldn't dismiss — users
    pressing Enter reflexively would otherwise fail the sudo call."""
    from textual.app import App

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(
                SudoPasswordModal(action_label="x", title="Test"),
                self._capture,
            )

        def _capture(self, value):
            self._captured = value

    host = _Host()
    async with host.run_test() as pilot:
        await pilot.pause()
        # No password typed.
        await pilot.press("enter")
        await pilot.pause()
        # Modal should still be on top — no dismiss fired.
        assert isinstance(host.screen, SudoPasswordModal)
        assert not hasattr(host, "_captured")


async def test_modal_surfaces_initial_error():
    """When re-prompting after a failed auth attempt, the error label
    should be visible (not display:none) and carry the given message."""
    from textual.app import App

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(
                SudoPasswordModal(
                    action_label="x",
                    title="T",
                    initial_error="Incorrect password.",
                ),
                lambda _: None,
            )

    host = _Host()
    async with host.run_test() as pilot:
        await pilot.pause()
        err_label = host.screen.query_one("#error")
        # Visible: -hidden class was applied only when initial_error is empty.
        assert "-hidden" not in err_label.classes
        # Content: Label stores its text on ._content (Textual 8.x).
        text = str(err_label.render())
        assert "Incorrect" in text


async def test_modal_hides_error_label_by_default():
    """Without initial_error, the error label must carry the -hidden class
    so it doesn't eat vertical space in the dialog."""
    from textual.app import App

    class _Host(App):
        def on_mount(self) -> None:
            self.push_screen(
                SudoPasswordModal(action_label="x", title="T"),
                lambda _: None,
            )

    host = _Host()
    async with host.run_test() as pilot:
        await pilot.pause()
        err_label = host.screen.query_one("#error")
        assert "-hidden" in err_label.classes
