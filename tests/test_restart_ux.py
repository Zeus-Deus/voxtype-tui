"""Pilot tests for the daemon-restart UX flow.

Covers:
- RestartingPill visibility through the lifecycle (hidden → visible → hidden)
- StalePill mutual exclusion (hidden while restart in flight)
- "⟳ starting" status label during the gap between systemctl-active and
  daemon-actually-ready
- Quit blocked while restart in flight
- Re-entry guard on _do_restart
- ctrl+shift+r short-circuits with the in-progress message instead of
  the misleading "already up to date"
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from textual.widgets import Static

from voxtype_tui import voxtype_cli
from voxtype_tui.app import RestartingPill, StalePill, VoxtypeTUI

from .conftest import FIXTURES


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)

    async def _active():
        return True

    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: True)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _active)
    return cfg, side


def _patch_restart(monkeypatch, *, ok: bool = True, msg: str = "voxtype restarted",
                   delay: float = 0.0) -> dict:
    """Stub restart_daemon_async to optionally take `delay` seconds.

    Returning the call-counter dict lets tests assert how many restarts
    actually fired (re-entry guard test cares about this)."""
    counter = {"n": 0}

    async def fake_restart():
        counter["n"] += 1
        if delay:
            await asyncio.sleep(delay)
        return ok, msg

    monkeypatch.setattr(voxtype_cli, "restart_daemon_async", fake_restart)
    return counter


def _patch_ready(monkeypatch, *, ready: bool = True, delay: float = 0.05) -> None:
    """Stub the readiness wait. Real helper polls the state file; tests
    just say "the daemon takes <delay> seconds to come back, then it's
    <ready>"."""
    async def fake_wait(timeout: float = 20.0, poll_interval: float = 0.15):
        if delay:
            await asyncio.sleep(delay)
        return ready

    monkeypatch.setattr(voxtype_cli, "wait_for_daemon_ready_async", fake_wait)


# ---------------------------------------------------------------------------
# RestartingPill lifecycle
# ---------------------------------------------------------------------------

async def test_restarting_pill_hidden_at_rest(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pill = app.query_one("#daemon-restarting", RestartingPill)
        assert not pill.has_class("visible")
        assert app.restart_in_progress is False


async def test_restarting_pill_visible_during_restart(tmp_env, monkeypatch):
    """While _do_restart is awaiting the readiness wait, the pill must
    be visible — that's the user-facing signal that we're working."""
    cfg, side = tmp_env
    _patch_restart(monkeypatch)
    # Long-ish readiness delay so we can observe the in-flight state.
    _patch_ready(monkeypatch, ready=True, delay=0.3)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.daemon_stale = True
        app.refresh_stale_pill()
        await pilot.pause()

        # Kick off the restart but don't await it — let it run in the
        # background while we sample the pill state.
        task = asyncio.create_task(app._do_restart())
        # Yield control so the task starts and flips restart_in_progress.
        await asyncio.sleep(0.05)
        await pilot.pause()

        assert app.restart_in_progress is True
        pill = app.query_one("#daemon-restarting", RestartingPill)
        assert pill.has_class("visible"), "in-progress pill should be up mid-restart"

        await task
        await pilot.pause()

        assert app.restart_in_progress is False
        assert not pill.has_class("visible"), "pill should clear once restart completes"


async def test_stale_pill_hidden_while_restart_in_progress(tmp_env, monkeypatch):
    """Showing both 'restart needed' and 'restarting…' simultaneously
    sends contradictory signals. While we're mid-restart the stale pill
    must be hidden, even if state.daemon_stale is briefly still True."""
    cfg, side = tmp_env
    _patch_restart(monkeypatch)
    _patch_ready(monkeypatch, ready=True, delay=0.3)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.daemon_stale = True
        app.refresh_stale_pill()
        await pilot.pause()
        stale = app.query_one("#daemon-stale", StalePill)
        assert stale.has_class("visible"), "stale pill should be up before restart"

        task = asyncio.create_task(app._do_restart())
        await asyncio.sleep(0.05)
        await pilot.pause()

        assert not stale.has_class("visible"), \
            "stale pill must hide while restart is in flight"

        await task
        await pilot.pause()
        # After restart, stale was cleared by _do_restart, so pill stays hidden.
        assert not stale.has_class("visible")


# ---------------------------------------------------------------------------
# Status label
# ---------------------------------------------------------------------------

async def test_status_shows_starting_during_restart_when_state_unknown(tmp_env, monkeypatch):
    """The state file disappears while the daemon is down. Without our
    change the header would read '○ no-daemon', which made users think
    the restart had failed. It should read '⟳ starting' instead."""
    cfg, side = tmp_env
    _patch_restart(monkeypatch)
    _patch_ready(monkeypatch, ready=True, delay=0.3)
    monkeypatch.setattr(voxtype_cli, "read_state", lambda: None)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.daemon_stale = True
        app.refresh_stale_pill()
        await pilot.pause()

        task = asyncio.create_task(app._do_restart())
        await asyncio.sleep(0.05)
        await pilot.pause()

        status = app.query_one("#status", Static)
        rendered = str(status.content)
        assert "starting" in rendered, \
            f"expected 'starting' in status during restart, got {rendered!r}"

        await task
        await pilot.pause()


async def test_status_resumes_normal_label_after_restart_completes(tmp_env, monkeypatch):
    """Once the daemon writes its state file, the status label drops the
    'starting' text and shows the real state with its icon."""
    cfg, side = tmp_env
    _patch_restart(monkeypatch)
    _patch_ready(monkeypatch, ready=True, delay=0.05)
    # Daemon comes back as 'idle' once the readiness wait returns.
    state_holder = {"v": None}
    monkeypatch.setattr(voxtype_cli, "read_state", lambda: state_holder["v"])

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.daemon_stale = True
        app.refresh_stale_pill()
        await pilot.pause()

        # Once readiness wait finishes, _do_restart calls poll_daemon_state
        # which reads the new state. Set the holder so that read returns idle.
        async def _start_then_idle():
            await asyncio.sleep(0.02)
            state_holder["v"] = "idle"
            return True

        # Override readiness to flip the state file under us partway through.
        monkeypatch.setattr(
            voxtype_cli, "wait_for_daemon_ready_async",
            lambda timeout=20.0, poll_interval=0.15: _start_then_idle(),
        )

        await app._do_restart()
        await pilot.pause()

        status = app.query_one("#status", Static)
        rendered = str(status.content)
        assert "idle" in rendered
        assert "starting" not in rendered


# ---------------------------------------------------------------------------
# Quit guard
# ---------------------------------------------------------------------------

async def test_quit_blocked_while_restart_in_progress(tmp_env, monkeypatch):
    """Pressing Q during a restart used to silently queue self.exit() and
    only take effect once systemctl returned. Now it surfaces a notice
    and stays open."""
    cfg, side = tmp_env
    _patch_restart(monkeypatch)
    _patch_ready(monkeypatch, ready=True, delay=0.3)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.daemon_stale = True
        app.refresh_stale_pill()
        await pilot.pause()

        exited = {"called": False}
        orig_exit = app.exit

        def track_exit(*args, **kwargs):
            exited["called"] = True
            return orig_exit(*args, **kwargs)

        app.exit = track_exit  # type: ignore[method-assign]

        task = asyncio.create_task(app._do_restart())
        await asyncio.sleep(0.05)
        await pilot.pause()

        assert app.restart_in_progress is True
        app.action_request_quit()
        await pilot.pause()
        assert exited["called"] is False, \
            "quit must not exit while a restart is in flight"

        await task
        await pilot.pause()
        # After restart finishes, quit works normally (state is clean).
        app.action_request_quit()
        await pilot.pause()
        assert exited["called"] is True


# ---------------------------------------------------------------------------
# Re-entry guard
# ---------------------------------------------------------------------------

async def test_double_restart_only_runs_once(tmp_env, monkeypatch):
    """Hammering ctrl+shift+r mid-restart used to trigger 'Daemon is
    already up to date' (because daemon_stale was already cleared) — now
    it shows the in-progress message AND, more importantly, doesn't fire
    a second systemctl restart in parallel."""
    cfg, side = tmp_env
    counter = _patch_restart(monkeypatch)
    _patch_ready(monkeypatch, ready=True, delay=0.2)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.daemon_stale = True
        app.refresh_stale_pill()
        await pilot.pause()

        # First restart in flight.
        task1 = asyncio.create_task(app._do_restart())
        await asyncio.sleep(0.05)
        await pilot.pause()
        assert app.restart_in_progress is True

        # Second invocation while the first is in flight — must short-circuit.
        await app.action_restart_daemon()
        await pilot.pause()

        await task1
        await pilot.pause()

        assert counter["n"] == 1, \
            f"only one systemctl restart should fire, got {counter['n']}"


async def test_restart_warns_on_readiness_timeout(tmp_env, monkeypatch):
    """If systemctl succeeds but the daemon never writes its ready state,
    the user must hear about it — not see a green 'restarted' toast."""
    cfg, side = tmp_env
    _patch_restart(monkeypatch, ok=True, msg="voxtype restarted")
    _patch_ready(monkeypatch, ready=False, delay=0.05)

    notifications: list[tuple[str, str]] = []
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.daemon_stale = True
        app.refresh_stale_pill()
        await pilot.pause()

        orig_notify = app.notify

        def track_notify(message, *args, severity="information", **kwargs):
            notifications.append((severity, str(message)))
            return orig_notify(message, *args, severity=severity, **kwargs)

        app.notify = track_notify  # type: ignore[method-assign]

        await app._do_restart()
        await pilot.pause()

        warnings = [m for s, m in notifications if s == "warning"]
        assert any("didn't report ready" in w for w in warnings), \
            f"expected a readiness-timeout warning, got {notifications}"
        # And the in-progress flag is released so the next restart can run.
        assert app.restart_in_progress is False


async def test_restart_clears_stale_pill_immediately_after_systemctl(
    tmp_env, monkeypatch,
):
    """Q3 regression: the StalePill must hide as soon as systemctl
    confirms the restart kicked off — NOT 5–15s later when the
    readiness wait finishes. The user's screenshot showed the warning
    pill still visible after they pressed "Restart now"; the fix is an
    explicit `refresh_stale_pill()` call right after we set
    `state.daemon_stale = False`."""
    cfg, side = tmp_env
    _patch_restart(monkeypatch, ok=True)
    # Make readiness wait take a noticeable amount of time so we can
    # sample the pill state in between systemctl's return and ready.
    _patch_ready(monkeypatch, ready=True, delay=0.4)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.daemon_stale = True
        app.refresh_stale_pill()
        await pilot.pause()
        stale = app.query_one("#daemon-stale", StalePill)
        assert stale.has_class("visible")

        task = asyncio.create_task(app._do_restart())
        # Yield enough that systemctl returned and we cleared
        # daemon_stale + refreshed the pill, but the readiness wait is
        # still pending.
        await asyncio.sleep(0.1)
        await pilot.pause()

        assert app.state.daemon_stale is False
        assert not stale.has_class("visible"), \
            "stale pill must hide immediately when daemon_stale flips False"

        await task
        await pilot.pause()
        assert not stale.has_class("visible")


async def test_restart_failure_releases_in_progress_flag(tmp_env, monkeypatch):
    """systemctl-level failure: in-progress flag must clear so the user
    can try again and the quit guard isn't stuck open."""
    cfg, side = tmp_env
    _patch_restart(monkeypatch, ok=False, msg="systemd exploded")
    # Readiness wait shouldn't even be called on systemctl failure.
    called = {"ready": False}

    async def fake_wait(timeout: float = 20.0, poll_interval: float = 0.15):
        called["ready"] = True
        return False

    monkeypatch.setattr(voxtype_cli, "wait_for_daemon_ready_async", fake_wait)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.daemon_stale = True
        app.refresh_stale_pill()
        await pilot.pause()

        await app._do_restart()
        await pilot.pause()

        assert app.restart_in_progress is False
        assert called["ready"] is False, \
            "readiness wait shouldn't run when systemctl failed"
