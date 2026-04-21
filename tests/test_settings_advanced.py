"""VAD, Post-processing, and Remote backend sections — the three
collapsed-by-default sections for less-used features."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from textual.widgets import Input, Switch

from voxtype_tui import config, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.settings import SettingsPane
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


# ---- VAD ----

async def test_vad_enable_and_threshold(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        baseline = config.load(cfg)

        pane.query_one("#settings-vad-enabled", Switch).value = True
        await pilot.pause()
        pane.query_one("#settings-vad-threshold", Input).value = "0.5"
        await pilot.pause()

        assert bool(app.state.doc["vad"]["enabled"]) is True
        assert float(app.state.doc["vad"]["threshold"]) == 0.5

        # VAD changes are restart-sensitive
        diff = config.diff_restart_sensitive(baseline, app.state.doc)
        assert "vad.enabled" in diff
        assert "vad.threshold" in diff


async def test_vad_threshold_rejects_out_of_range(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-vad-threshold", Input).value = "0.4"
        await pilot.pause()

        pane.query_one("#settings-vad-threshold", Input).value = "1.5"
        await pilot.pause()

        # 1.5 is out of range; previous 0.4 should still be there
        assert float(app.state.doc["vad"]["threshold"]) == 0.4


# ---- Post-processing ----

async def test_post_process_command_and_timeout(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-post-command", Input).value = (
            "ollama run llama3.2:1b 'clean this up'"
        )
        await pilot.pause()
        pane.query_one("#settings-post-timeout", Input).value = "20000"
        await pilot.pause()

        post = app.state.doc["output"]["post_process"]
        assert "ollama run" in str(post["command"])
        assert int(post["timeout_ms"]) == 20000

        # Not restart-sensitive
        baseline = config.load(cfg)
        # Reload to get the post-save state — post_process changes were
        # written via set_setting, so compare before-write vs after
        # (use baseline before any mutation)
        diff = config.diff_restart_sensitive(baseline, app.state.doc)
        assert diff == []


async def test_post_process_section_persists_through_save(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-post-command", Input).value = "cat"
        pane.query_one("#settings-post-timeout", Input).value = "5000"
        await pilot.pause()

        await pilot.press("ctrl+s")
        await pilot.pause()

    reloaded = config.load(cfg)
    assert str(reloaded["output"]["post_process"]["command"]) == "cat"
    assert int(reloaded["output"]["post_process"]["timeout_ms"]) == 5000


# ---- Remote backend ----

async def test_remote_backend_fields(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-remote-endpoint", Input).value = (
            "http://192.168.1.42:8080"
        )
        pane.query_one("#settings-remote-model", Input).value = "whisper-1"
        pane.query_one("#settings-remote-timeout", Input).value = "45"
        await pilot.pause()

        assert str(app.state.doc["whisper"]["remote_endpoint"]) == "http://192.168.1.42:8080"
        assert str(app.state.doc["whisper"]["remote_model"]) == "whisper-1"
        assert int(app.state.doc["whisper"]["remote_timeout_secs"]) == 45


async def test_remote_endpoint_is_restart_sensitive(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        baseline = config.load(cfg)
        pane.query_one("#settings-remote-endpoint", Input).value = "https://api.openai.com"
        await pilot.pause()

        diff = config.diff_restart_sensitive(baseline, app.state.doc)
        assert "whisper.remote_endpoint" in diff


async def test_api_key_input_is_password_masked(tmp_env):
    """The API key Input has password=True so keystrokes render as bullets
    rather than the literal characters (important if someone is screen-
    sharing or screencasting)."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)
        api = pane.query_one("#settings-remote-api-key", Input)
        assert api.password is True


async def test_api_key_empty_clears_it(tmp_env):
    """Unlike other Inputs that skip empty values, the API key should accept
    empty-string writes so users can wipe a previously-set key."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        api = pane.query_one("#settings-remote-api-key", Input)
        api.value = "sk-abcdef"
        await pilot.pause()
        assert str(app.state.doc["whisper"]["remote_api_key"]) == "sk-abcdef"

        api.value = ""
        await pilot.pause()
        assert str(app.state.doc["whisper"]["remote_api_key"]) == ""
