"""Audio section of the Settings pane — device Select (pactl-populated),
max duration, and audio feedback toggle/theme/volume."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from textual.widgets import Input, Select, Switch

from voxtype_tui import config, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.settings import (
    CUSTOM_AUDIO_DEVICE,
    SettingsPane,
    enumerate_audio_devices_sync,
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


def test_enumerate_audio_fallback_when_pactl_missing(monkeypatch) -> None:
    """If pactl isn't installed, we still return a usable Select list."""
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "pactl" else "/usr/bin/" + name)
    options = enumerate_audio_devices_sync()
    values = [v for _, v in options]
    assert "default" in values
    assert CUSTOM_AUDIO_DEVICE in values


def test_enumerate_audio_filters_to_alsa_input(monkeypatch) -> None:
    """Only rows starting with `alsa_input.` become options; monitor sinks
    and other rows are filtered out."""
    import subprocess

    class R:
        returncode = 0
        stdout = (
            "57\talsa_output.hdmi.monitor\tPipeWire\ts32le\tSUSPENDED\n"
            "58\talsa_input.usb-Generic_USB_Audio-00.HiFi__Mic__source\tPipeWire\ts24le\tSUSPENDED\n"
            "59\talsa_output.usb-X.monitor\tPipeWire\ts32le\tSUSPENDED\n"
            "60\talsa_input.usb-SteelSeries_Arctis_7.mono-chat\tPipeWire\ts16le\tSUSPENDED\n"
        )

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/pactl" if name == "pactl" else None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: R())

    options = enumerate_audio_devices_sync()
    values = [v for _, v in options]
    assert "default" in values
    assert "alsa_input.usb-Generic_USB_Audio-00.HiFi__Mic__source" in values
    assert "alsa_input.usb-SteelSeries_Arctis_7.mono-chat" in values
    # monitors excluded
    assert not any(".monitor" in v for v in values)
    assert CUSTOM_AUDIO_DEVICE in values


async def test_audio_hydrates_from_stock(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        # Device gets populated asynchronously; give the task a tick
        await pilot.pause()
        await pilot.pause()

        # stock has device = "default", max_duration = 60, feedback off
        assert pane.query_one("#settings-audio-device", Select).value == "default"
        assert pane.query_one("#settings-audio-maxdur", Input).value == "60"
        assert pane.query_one("#settings-audio-feedback-enabled", Switch).value is False
        assert app.state.config_dirty is False


async def test_max_duration_int_parse(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        inp = pane.query_one("#settings-audio-maxdur", Input)
        inp.value = "120"
        await pilot.pause()
        assert int(app.state.doc["audio"]["max_duration_secs"]) == 120

        # Invalid value is a no-op — doesn't corrupt the previously-set value
        inp.value = "abc"
        await pilot.pause()
        assert int(app.state.doc["audio"]["max_duration_secs"]) == 120


async def test_max_duration_not_restart_sensitive(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        baseline = config.load(cfg)
        pane.query_one("#settings-audio-maxdur", Input).value = "90"
        await pilot.pause()

        diff = config.diff_restart_sensitive(baseline, app.state.doc)
        assert diff == []


async def test_audio_feedback_toggle_writes_explicit_bool(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        pane.query_one("#settings-audio-feedback-enabled", Switch).value = True
        await pilot.pause()

        assert bool(app.state.doc["audio"]["feedback"]["enabled"]) is True
        assert app.state.config_dirty is True


async def test_feedback_volume_range_enforced(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)

        # Out-of-range is silently ignored (save validator would reject anyway)
        inp = pane.query_one("#settings-audio-feedback-volume", Input)
        inp.value = "2.0"
        await pilot.pause()
        feedback = app.state.doc.get("audio", {}).get("feedback", {})
        # Shouldn't have been written
        assert "volume" not in feedback or feedback.get("volume") != 2.0

        # Valid float in range lands
        inp.value = "0.5"
        await pilot.pause()
        assert float(app.state.doc["audio"]["feedback"]["volume"]) == 0.5


async def test_custom_audio_device_path_is_written(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)
        await pilot.pause()
        await pilot.pause()

        pane.query_one("#settings-audio-device", Select).value = CUSTOM_AUDIO_DEVICE
        await pilot.pause()
        custom = pane.query_one("#settings-audio-custom", Input)
        custom.value = "alsa_input.platform-snd_aloop.0.analog-stereo"
        await pilot.pause()

        assert (
            str(app.state.doc["audio"]["device"])
            == "alsa_input.platform-snd_aloop.0.analog-stereo"
        )


async def test_device_change_is_restart_sensitive(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_settings(pilot, app)
        pane = app.query_one(SettingsPane)
        await pilot.pause()
        await pilot.pause()

        baseline = config.load(cfg)
        pane.query_one("#settings-audio-device", Select).value = CUSTOM_AUDIO_DEVICE
        await pilot.pause()
        pane.query_one("#settings-audio-custom", Input).value = "alsa_input.something-else"
        await pilot.pause()

        diff = config.diff_restart_sensitive(baseline, app.state.doc)
        assert "audio.device" in diff
