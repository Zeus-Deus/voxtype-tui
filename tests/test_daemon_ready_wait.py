"""Tests for `voxtype_cli.wait_for_daemon_ready_async`.

The helper exists because `systemctl restart voxtype` returns the moment
the unit is `active`, but voxtype still needs to load the model before it
can serve. Polling the state file is the honest signal — these tests
cover the immediate-ready, delayed-ready, and timeout paths.
"""
from __future__ import annotations

from voxtype_tui import voxtype_cli


async def test_returns_true_immediately_when_already_ready(monkeypatch) -> None:
    monkeypatch.setattr(voxtype_cli, "read_state", lambda: "idle")
    ok = await voxtype_cli.wait_for_daemon_ready_async(
        timeout=1.0, poll_interval=0.05
    )
    assert ok is True


async def test_returns_true_when_state_appears_after_some_polls(monkeypatch) -> None:
    """Simulates the realistic case: state file is missing for a while as
    the daemon loads its model, then flips to idle."""
    counter = {"n": 0}

    def fake_read_state() -> str | None:
        counter["n"] += 1
        if counter["n"] < 3:
            return None  # state file not yet written
        return "idle"

    monkeypatch.setattr(voxtype_cli, "read_state", fake_read_state)
    ok = await voxtype_cli.wait_for_daemon_ready_async(
        timeout=2.0, poll_interval=0.01
    )
    assert ok is True
    assert counter["n"] >= 3


async def test_returns_false_on_timeout(monkeypatch) -> None:
    """Daemon never comes back. Helper should return False, not hang."""
    monkeypatch.setattr(voxtype_cli, "read_state", lambda: None)
    ok = await voxtype_cli.wait_for_daemon_ready_async(
        timeout=0.2, poll_interval=0.05
    )
    assert ok is False


async def test_recording_state_also_counts_as_ready(monkeypatch) -> None:
    """If the user happens to press the hotkey the instant the daemon
    finishes loading, state could land on 'recording' or 'transcribing'
    rather than 'idle'. Both should still satisfy ready."""
    monkeypatch.setattr(voxtype_cli, "read_state", lambda: "recording")
    ok = await voxtype_cli.wait_for_daemon_ready_async(
        timeout=1.0, poll_interval=0.05
    )
    assert ok is True


async def test_unknown_state_string_does_not_count_as_ready(monkeypatch) -> None:
    """Defensive: if voxtype ever writes a new state we don't recognize,
    we should NOT prematurely declare ready. Better to time out and warn
    than to lie."""
    monkeypatch.setattr(voxtype_cli, "read_state", lambda: "loading-model")
    ok = await voxtype_cli.wait_for_daemon_ready_async(
        timeout=0.2, poll_interval=0.05
    )
    assert ok is False


def test_daemon_ready_states_constant_is_correct() -> None:
    """Lock the contract — these are the strings voxtype's daemon writes
    to its state file once it's serving. Any change here is a behavior
    change for the readiness wait."""
    assert voxtype_cli.DAEMON_READY_STATES == (
        "idle", "recording", "transcribing"
    )
