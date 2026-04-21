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
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
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
        inp = app.query_one("#add", Input)
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
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)

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
