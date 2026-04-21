"""End-to-end Pilot tests for the shell: load, dirty, save, reload, banner.

These tests confirm the cross-cutting concerns (dirty tracking, status
poller wiring, save flow, reconcile banner) work before we build real tab
content on top of them.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from voxtype_tui import config, sidecar, voxtype_cli
from textual.widgets import Static

from voxtype_tui.app import ReconcileBanner, VoxtypeTUI
from .conftest import FIXTURES


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Tempdir config + sidecar. Disables the real systemctl / restart path
    so the save flow doesn't reach the system."""
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)
    # Pretend the daemon is inactive → skip restart modal in save flow
    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)
    return cfg, side


async def test_shell_loads_and_shows_clean_state(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.state is not None
        assert app.state.dirty is False
        # The #dirty Static widget should exist and be rendered.
        assert app.query_one("#dirty", Static) is not None


async def test_fake_mutation_flips_dirty_then_save_clears(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.state.dirty is True

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.state.dirty is False

    reloaded = config.load(cfg)
    prompt = config.get_initial_prompt(reloaded)
    assert prompt == "TestWord0"

    sc = sidecar.load(side)
    assert [v.phrase for v in sc.vocabulary] == ["TestWord0"]


async def test_tab_switching_via_digit_keys(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one("#tabs")
        assert tabs.active == "vocabulary"

        await pilot.press("2")
        await pilot.pause()
        assert tabs.active == "dictionary"

        await pilot.press("3")
        await pilot.pause()
        assert tabs.active == "settings"

        await pilot.press("1")
        await pilot.pause()
        assert tabs.active == "vocabulary"


async def test_digit_keys_do_not_switch_tabs_while_input_focused(tmp_env):
    """Regression: typing a number into the Add input must insert the
    character, not jump tabs."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Focus the Vocabulary Add input
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        tabs = app.query_one("#tabs")
        assert tabs.active == "vocabulary", "digit keys should not switch tabs while typing"
        from textual.widgets import Input
        inp = app.query_one("#vocab-add", Input)
        assert "2" in inp.value


async def test_reconcile_banner_shows_for_orphaned_sidecar(tmp_path: Path, monkeypatch):
    """Pre-seed a sidecar with a replacement that isn't in config → reconcile
    drops it and produces a warning → banner becomes visible."""
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)
    side.write_text(json.dumps({
        "version": 1,
        "vocabulary": [],
        "replacements": [
            {"from_text": "ghost_rule", "category": "Command",
             "added_at": "2026-01-01T00:00:00+00:00"}
        ],
    }))
    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one(ReconcileBanner)
        assert banner.has_class("visible"), "banner should be visible after orphan reconcile"
        # Dismiss via button
        await pilot.click("#dismiss")
        await pilot.pause()
        assert not banner.has_class("visible")


async def test_save_with_invalid_config_is_rejected(tmp_env):
    """If somehow state ends up with a value voxtype rejects, safe_save
    raises ValidationError and the on-disk file is preserved."""
    cfg, side = tmp_env
    original_bytes = cfg.read_bytes()

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Inject a bogus engine value
        app.state.set_setting("engine", "not-a-real-engine")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

    # File on disk must be untouched
    assert cfg.read_bytes() == original_bytes


async def test_pill_hidden_by_default(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        from voxtype_tui.app import StalePill
        pill = app.query_one("#daemon-stale", StalePill)
        assert not pill.has_class("visible")
        assert app.state.daemon_stale is False


async def test_restart_sensitive_save_sets_stale_and_shows_pill(tmp_env):
    """Saving a restart-sensitive change makes daemon_stale True and the
    pill becomes visible even when the daemon's inactive (no modal path)."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Add a vocab word — whisper.initial_prompt is restart-sensitive
        app.state.add_vocab("NewWord")
        app.refresh_dirty()
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert app.state.daemon_stale is True
        from voxtype_tui.app import StalePill
        pill = app.query_one("#daemon-stale", StalePill)
        assert pill.has_class("visible")


async def test_modal_fires_only_on_first_stale_transition(tmp_env, monkeypatch):
    """First restart-sensitive save shows the modal; subsequent saves during
    an already-stale session just update the pill + toast, no modal spam."""
    cfg, side = tmp_env

    # The modal only appears when the daemon is reachable — override the
    # tmp_env default so the modal path is actually exercised.
    async def fake_active():
        return True
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", fake_active)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)

    modal_opens: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        orig_push = app.push_screen

        def track_push(screen, *args, **kwargs):
            modal_opens.append(type(screen).__name__)
            return orig_push(screen, *args, **kwargs)

        app.push_screen = track_push  # type: ignore[method-assign]

        # First restart-sensitive save — modal expected
        app.state.add_vocab("Alpha")
        app.refresh_dirty()
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        from voxtype_tui.app import RestartModal
        if isinstance(app.screen, RestartModal):
            await pilot.press("escape")
            await pilot.pause()

        first_wave_modals = [m for m in modal_opens if m == "RestartModal"]
        assert len(first_wave_modals) == 1
        assert app.state.daemon_stale is True

        # Second restart-sensitive save — no new modal
        app.state.add_vocab("Beta")
        app.refresh_dirty()
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        # still only one RestartModal across both saves
        second_wave_modals = [m for m in modal_opens if m == "RestartModal"]
        assert len(second_wave_modals) == 1
        assert app.state.daemon_stale is True


async def test_ctrl_shift_r_triggers_restart(tmp_env, monkeypatch):
    """Pressing ctrl+shift+r from anywhere invokes restart_daemon_async and
    clears daemon_stale on success."""
    cfg, side = tmp_env
    called = []

    async def fake_active():
        return True

    async def fake_restart():
        called.append("restart")
        return True, "voxtype restarted"

    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", fake_active)
    monkeypatch.setattr(voxtype_cli, "restart_daemon_async", fake_restart)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.daemon_stale = True
        app.refresh_stale_pill()
        await pilot.pause()

        await pilot.press("ctrl+shift+r")
        await pilot.pause()

    assert called == ["restart"]
    assert app.state.daemon_stale is False


async def test_ctrl_shift_r_noop_when_not_stale(tmp_env, monkeypatch):
    """No damage if the user hammers ctrl+shift+r when the daemon is fine."""
    cfg, side = tmp_env
    called = []

    async def fake_restart():
        called.append("restart")
        return True, ""

    monkeypatch.setattr(voxtype_cli, "restart_daemon_async", fake_restart)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.state.daemon_stale is False
        await pilot.press("ctrl+shift+r")
        await pilot.pause()
    assert called == []


async def test_ctrl_shift_r_clears_pill_when_daemon_is_down(tmp_env, monkeypatch):
    """If ctrl+shift+r fires but the daemon isn't active, we clear
    daemon_stale to stop lying with the pill — there's nothing to restart."""
    cfg, side = tmp_env

    async def fake_inactive():
        return False

    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", fake_inactive)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.daemon_stale = True
        app.refresh_stale_pill()
        await pilot.pause()

        await pilot.press("ctrl+shift+r")
        await pilot.pause()
    assert app.state.daemon_stale is False


async def test_daemon_status_poll_does_not_crash(tmp_env):
    """Even if the state file doesn't exist (tests run outside the daemon's
    user), the poller should update to 'no-daemon' without raising."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Force a poll — it may have already ticked, but this should be safe
        app.poll_daemon_state()
        await pilot.pause()
        assert app.query_one("#status", Static) is not None
        # daemon_state should have been polled; just verify it's a string
        assert isinstance(app.daemon_state, str)
