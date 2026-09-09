"""Unit tests for `voxtype_cli.restart_daemon` — specifically the
"prove it actually restarted" check.

`systemctl --user restart` returns 0 whether or not a new process came
up (and even, in weird environments, when it silently did nothing). The
old implementation trusted that exit code alone, so the UI could show a
green "restarted" toast over a daemon that never cycled. These tests pin
the contract: success requires the unit's (MainPID, start timestamp) to
differ before vs after.

subprocess.run is stubbed at the module level; no real systemctl calls.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from voxtype_tui import voxtype_cli


def _fake_run(monkeypatch, *, identities: list[str], restart_rc: int = 0,
              restart_stderr: str = "", show_rc: int = 0) -> list[list[str]]:
    """Stub subprocess.run. `identities` is consumed one entry per
    `systemctl show` call (stdout for the before/after probes). Returns
    the list of argv lists actually invoked so tests can assert order."""
    calls: list[list[str]] = []
    queue = list(identities)

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "show"]:
            out = queue.pop(0) if queue else ""
            return SimpleNamespace(returncode=show_rc, stdout=out, stderr="")
        if argv[:3] == ["systemctl", "--user", "restart"]:
            return SimpleNamespace(
                returncode=restart_rc, stdout="", stderr=restart_stderr,
            )
        raise AssertionError(f"unexpected argv {argv}")

    monkeypatch.setattr(voxtype_cli.shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(voxtype_cli.subprocess, "run", run)
    return calls


def test_restart_succeeds_when_pid_changes(monkeypatch):
    calls = _fake_run(monkeypatch, identities=["2027\n12345\n", "9981\n67890\n"])
    ok, msg = voxtype_cli.restart_daemon()
    assert ok is True
    assert msg == "voxtype restarted"
    # show → restart → show
    assert [c[2] for c in calls] == ["show", "restart", "show"]


def test_restart_fails_when_pid_and_start_unchanged(monkeypatch):
    """The case observed on a live machine: systemctl exits 0,
    MainPID=2027 before and after, NRestarts=0. Must be reported as a
    failure with the PID in the message so the user can verify."""
    _fake_run(monkeypatch, identities=["2027\n12345\n", "2027\n12345\n"])
    ok, msg = voxtype_cli.restart_daemon()
    assert ok is False
    assert "was not restarted" in msg
    assert "2027" in msg


def test_restart_succeeds_on_pid_reuse_if_start_time_moved(monkeypatch):
    """PID reuse is theoretically possible; the monotonic start stamp is
    the tiebreaker."""
    _fake_run(monkeypatch, identities=["2027\n12345\n", "2027\n99999\n"])
    ok, _ = voxtype_cli.restart_daemon()
    assert ok is True


def test_restart_falls_back_to_exit_code_when_identity_unreadable(monkeypatch):
    """If `systemctl show` itself fails we can't prove anything either
    way; don't block the restart on it — trust the exit code as before."""
    _fake_run(monkeypatch, identities=["", ""], show_rc=1)
    ok, msg = voxtype_cli.restart_daemon()
    assert ok is True
    assert msg == "voxtype restarted"


def test_restart_reports_systemctl_failure(monkeypatch):
    _fake_run(
        monkeypatch, identities=["2027\n12345\n"],
        restart_rc=1, restart_stderr="Failed to restart voxtype.service: boom\n",
    )
    ok, msg = voxtype_cli.restart_daemon()
    assert ok is False
    assert "boom" in msg


def test_restart_reports_timeout(monkeypatch):
    def run(argv, **kwargs):
        if argv[:3] == ["systemctl", "--user", "show"]:
            return SimpleNamespace(returncode=0, stdout="2027\n1\n", stderr="")
        raise subprocess.TimeoutExpired(argv, 15)

    monkeypatch.setattr(voxtype_cli.shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(voxtype_cli.subprocess, "run", run)
    ok, msg = voxtype_cli.restart_daemon()
    assert ok is False
    assert msg == "restart timed out"


def test_daemon_identity_parses_two_lines(monkeypatch):
    _fake_run(monkeypatch, identities=["2027\n12345\n"])
    assert voxtype_cli.daemon_identity() == ("2027", "12345")


@pytest.mark.parametrize("stdout", ["", "2027\n"])
def test_daemon_identity_none_on_short_output(monkeypatch, stdout):
    _fake_run(monkeypatch, identities=[stdout])
    assert voxtype_cli.daemon_identity() is None


def test_daemon_identity_none_without_systemctl(monkeypatch):
    monkeypatch.setattr(voxtype_cli.shutil, "which", lambda _: None)
    assert voxtype_cli.daemon_identity() is None
    assert voxtype_cli.restart_daemon() == (False, "systemctl not available")
