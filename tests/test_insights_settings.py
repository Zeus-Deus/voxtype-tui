"""The voxtype keys added in the insights release: shown in the TUI, never
written by hydration, written with the right type on a real toggle."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from textual.widgets import Input, Select, Switch

from voxtype_tui import config, settings, voxtype_cli
from voxtype_tui.app import VoxtypeTUI

from .conftest import FIXTURES

NEW_PATHS = {
    "audio.pause_media", "audio.duck_media", "audio.duck_media_volume_percent",
    "audio.duck_media_fade_ms", "osd.enabled", "osd.frontend", "osd.layout", "osd.position",
    "osd.palette", "text.filter_filler_words", "output.restore_clipboard",
    "output.shift_enter_newlines", "output.notification.on_recording_start",
    "output.notification.on_recording_stop", "output.notification.on_transcription",
    "output.notification.on_no_speech",
}


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "minimal.toml", cfg)

    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)
    return cfg, side


def test_new_paths_are_restart_sensitive_and_written_literally():
    assert NEW_PATHS <= config.RESTART_SENSITIVE_PATHS
    src = Path(settings.__file__).read_text()
    for path in NEW_PATHS:
        assert f'set_setting("{path}"' in src or f'unset_setting("{path}"' in src, path


async def test_hydration_leaves_new_keys_absent_and_state_clean(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.press("3")
        for _ in range(4):
            await pilot.pause()
        assert app.state.config_dirty is False
        for path in NEW_PATHS:
            node = app.state.doc
            for part in path.split("."):
                node = node.get(part) if hasattr(node, "get") else None
            assert node is None, path
        pane = app.query_one(settings.SettingsPane)
        assert pane.query_one("#settings-notify-transcription", Switch).value is True
        assert pane.query_one("#settings-osd-layout", Select).value == "compact"


async def test_toggles_write_typed_values(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.pause()
        pane = app.query_one(settings.SettingsPane)
        pane.query_one("#settings-audio-duck-media", Switch).value = True
        pane.query_one("#settings-audio-duck-volume", Input).value = "20"
        pane.query_one("#settings-osd-position", Select).value = "top-right"
        pane.query_one("#settings-osd-palette", Select).value = "omarchy"
        for _ in range(3):
            await pilot.pause()
        doc = app.state.doc
        assert doc["audio"]["duck_media"] is True
        assert doc["audio"]["duck_media_volume_percent"] == 20
        assert doc["osd"]["position"] == "top-right"
        assert doc["osd"]["palette"] == "omarchy"
        pane.query_one("#settings-osd-palette", Select).value = settings.OSD_PALETTE_UNSET
        for _ in range(3):
            await pilot.pause()
        assert "palette" not in app.state.doc["osd"]
