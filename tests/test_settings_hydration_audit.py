"""Audit: when SettingsPane hydrates from a config that is MISSING a key
(notably whisper.language), do widget on_change handlers fire as a side
effect of the programmatic value-set, and do they pollute state with
their own default?

Repro target: a previous report claimed `language = "fr"` appeared in a
user's config without explicit interaction. The hypothesis is a Select
with a non-None default firing Changed during hydration and the handler
unconditionally writing the value back.

This test boots the TUI against a config with NO whisper.language key
and asserts that after mount + a few pauses, state remains clean.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from voxtype_tui import voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from .conftest import FIXTURES


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


async def test_no_language_key_stays_unset_after_settings_hydration(tmp_env):
    cfg, side = tmp_env

    # Sanity: fixture truly has no whisper.language
    text_before = cfg.read_text()
    assert "language" not in text_before

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Visit Settings tab so SettingsPane mounts and hydrates.
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        whisper = app.state.doc.get("whisper", {})
        language_value = whisper.get("language") if whisper else None
        dirty = app.state.config_dirty

        assert language_value is None, (
            f"BUG: hydration injected whisper.language={language_value!r} "
            f"into a config that originally had no language key. "
            f"config_dirty={dirty}"
        )
        assert dirty is False, (
            "BUG: simply mounting the Settings pane against a clean config "
            "marked it dirty. A subsequent ctrl+s would persist the widget "
            "defaults to disk."
        )


async def test_no_engine_default_pollution_when_engine_missing(tmp_env, tmp_path):
    """Variant: a config with NO top-level engine key shouldn't acquire
    'whisper' (or any other engine) as a side effect of mounting Settings."""
    cfg = tmp_path / "no-engine.toml"
    cfg.write_text(
        "[hotkey]\n"
        "key = \"SCROLLLOCK\"\n"
        "\n"
        "[audio]\n"
        "device = \"default\"\n"
        "sample_rate = 16000\n"
        "max_duration_secs = 60\n"
        "\n"
        "[whisper]\n"
        "model = \"base.en\"\n"
        "\n"
        "[output]\n"
        "mode = \"type\"\n"
    )
    side = tmp_path / "metadata.json"

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        engine_value = app.state.doc.get("engine")
        dirty = app.state.config_dirty
        assert engine_value is None, (
            f"BUG: hydration injected engine={engine_value!r} into a config "
            f"that originally had no top-level engine. dirty={dirty}"
        )
        assert dirty is False
