"""Hydration-time write detector for the Settings + Models panes.

Boots VoxtypeTUI against a tmp config that's missing optional `whisper`
fields (notably `language`) and verifies that mounting + tab-switching
into Settings or Models does not silently mutate the doc or flip
`config_dirty`. Mirrors the reverse case: a config that *has*
`whisper.language` set should also see zero programmatic writes during
hydration.

If this file fails, the failing assertion's message names the offending
dotted path so we can pin the unsolicited write to a specific widget
handler.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from voxtype_tui import voxtype_cli
from voxtype_tui.app import VoxtypeTUI


MINIMAL_NO_LANGUAGE = """\
[hotkey]
key = "SCROLLLOCK"

[audio]
device = "default"
sample_rate = 16000
max_duration_secs = 60

[whisper]
model = "base.en"

[output]
mode = "type"
"""


MINIMAL_WITH_LANGUAGE = """\
[hotkey]
key = "SCROLLLOCK"

[audio]
device = "default"
sample_rate = 16000
max_duration_secs = 60

[whisper]
model = "base.en"
language = "fr"

[output]
mode = "type"
"""


def _whisper_paths(doc) -> list[str]:
    """Every dotted key currently sitting under [whisper] in the doc.
    Used to flag any unsolicited keys the hydration path might create."""
    out: list[str] = []
    whisper = doc.get("whisper")
    if whisper is None:
        return out
    for k, v in whisper.items():
        out.append(f"whisper.{k}={v!r}")
    return out


@pytest.fixture
def silence_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _inactive() -> bool:
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)
    monkeypatch.setattr(voxtype_cli, "read_state", lambda: None)


def _write_cfg(tmp_path: Path, body: str) -> tuple[Path, Path]:
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    cfg.write_text(body)
    return cfg, side


async def test_settings_mount_no_language_does_not_write(
    tmp_path: Path, silence_daemon: None
) -> None:
    """Boot against a config with NO whisper.language. After visiting the
    Settings tab, the doc must still be untouched: no language key
    appeared, no other keys spawned, and config_dirty is False."""
    cfg, side = _write_cfg(tmp_path, MINIMAL_NO_LANGUAGE)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()

        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

        assert app.query_one("#tabs").active == "settings"
        assert app.state is not None

        whisper = app.state.doc.get("whisper") or {}
        language = whisper.get("language") if whisper else None
        keys_after = _whisper_paths(app.state.doc)

        assert language is None, (
            f"hydration wrote whisper.language={language!r} (was missing); "
            f"current [whisper] keys: {keys_after}"
        )
        assert app.state.doc["whisper"].get("model") == "base.en"
        assert app.state.config_dirty is False, (
            f"hydration dirtied config without user input; "
            f"current [whisper] keys: {keys_after}"
        )


async def test_models_mount_no_language_does_not_write(
    tmp_path: Path, silence_daemon: None
) -> None:
    """Same as above but lands on the Models tab (key '4'). Catches a
    Models-pane on_mount/sync_from_state that programmatically writes."""
    cfg, side = _write_cfg(tmp_path, MINIMAL_NO_LANGUAGE)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()

        await pilot.press("4")
        await pilot.pause()
        await pilot.pause()

        assert app.query_one("#tabs").active == "models"
        assert app.state is not None

        whisper = app.state.doc.get("whisper") or {}
        language = whisper.get("language") if whisper else None
        keys_after = _whisper_paths(app.state.doc)

        assert language is None, (
            f"models-tab hydration wrote whisper.language={language!r}; "
            f"current [whisper] keys: {keys_after}"
        )
        assert app.state.doc["whisper"].get("model") == "base.en"
        assert app.state.config_dirty is False, (
            f"models-tab hydration dirtied config without user input; "
            f"current [whisper] keys: {keys_after}"
        )


async def test_settings_mount_with_language_does_not_rewrite(
    tmp_path: Path, silence_daemon: None
) -> None:
    """Reverse case. Config carries whisper.language='fr' on disk. After
    settings hydration, the value must still be 'fr' AND the dirty flag
    must be False (no idempotent re-write through a non-equal-comparing
    code path)."""
    cfg, side = _write_cfg(tmp_path, MINIMAL_WITH_LANGUAGE)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()

        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

        assert app.query_one("#tabs").active == "settings"
        assert app.state is not None

        keys_after = _whisper_paths(app.state.doc)
        assert app.state.doc["whisper"]["language"] == "fr", (
            f"hydration mutated existing whisper.language; "
            f"current [whisper] keys: {keys_after}"
        )
        assert app.state.config_dirty is False, (
            f"hydration dirtied config despite no user input; "
            f"current [whisper] keys: {keys_after}"
        )
