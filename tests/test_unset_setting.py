"""Tests for `AppState.unset_setting` — the fix for the "can't clear
a stuck config value via the TUI" class of bug.

Origin: a stuck `whisper.language = "fr"` that survived every Ctrl+S
because the Settings tab's empty-input branch was a no-op. With
unset_setting wired in, clearing the Language Input removes the key
entirely and Voxtype's default ("en") kicks back in.
"""
from __future__ import annotations

from pathlib import Path

import tomlkit

from voxtype_tui.state import AppState


def _make_state(tmp_path: Path, body: str) -> AppState:
    cfg = tmp_path / "config.toml"
    cfg.write_text(body)
    side = tmp_path / "metadata.json"
    return AppState.load(cfg, side)


def test_unset_removes_leaf_and_flips_dirty(tmp_path: Path) -> None:
    state = _make_state(tmp_path, '[whisper]\nlanguage = "fr"\nmodel = "base.en"\n')
    state.config_dirty = False
    removed = state.unset_setting("whisper.language")
    assert removed is True
    assert state.config_dirty is True
    assert "language" not in state.doc["whisper"]
    # Sibling key survives — we only removed the targeted leaf.
    assert state.doc["whisper"]["model"] == "base.en"


def test_unset_missing_key_is_noop(tmp_path: Path) -> None:
    state = _make_state(tmp_path, '[whisper]\nmodel = "base.en"\n')
    state.config_dirty = False
    removed = state.unset_setting("whisper.language")
    assert removed is False
    assert state.config_dirty is False


def test_unset_missing_section_is_noop(tmp_path: Path) -> None:
    state = _make_state(tmp_path, "engine = \"whisper\"\n")
    state.config_dirty = False
    removed = state.unset_setting("whisper.language")
    assert removed is False
    assert state.config_dirty is False


def test_unset_cleans_up_empty_parent_section(tmp_path: Path) -> None:
    """Removing the only key under a section must remove the empty
    section header too — otherwise `[whisper]` sticks around with no
    keys and trips Voxtype's validator or looks weird in the file."""
    state = _make_state(tmp_path, '[whisper]\nlanguage = "fr"\n')
    state.unset_setting("whisper.language")
    assert "whisper" not in state.doc


def test_unset_preserves_nonempty_parent(tmp_path: Path) -> None:
    """Sibling keys keep the parent section alive."""
    state = _make_state(
        tmp_path,
        '[whisper]\nlanguage = "fr"\nmodel = "base.en"\n',
    )
    state.unset_setting("whisper.language")
    assert "whisper" in state.doc
    assert state.doc["whisper"]["model"] == "base.en"


def test_unset_then_save_writes_clean_config(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: unset + save → disk file no longer contains the key."""
    from voxtype_tui import config
    monkeypatch.setattr(config, "validate_with_voxtype", lambda p, timeout=10.0: (True, ""))
    state = _make_state(
        tmp_path,
        '[whisper]\nlanguage = "fr"\nmodel = "base.en"\n',
    )
    state.unset_setting("whisper.language")
    state.save()
    reloaded = tomlkit.parse((tmp_path / "config.toml").read_text())
    assert "language" not in reloaded.get("whisper", {})
    assert reloaded["whisper"]["model"] == "base.en"


def test_unset_nested_three_levels(tmp_path: Path) -> None:
    """Validates the `[output.post_process].command` depth of cleanup."""
    state = _make_state(
        tmp_path,
        '[output.post_process]\ncommand = "foo"\ntimeout_ms = 5000\n',
    )
    state.unset_setting("output.post_process.command")
    assert "command" not in state.doc["output"]["post_process"]
    assert state.doc["output"]["post_process"]["timeout_ms"] == 5000
    # Clearing the remaining key should strip the whole section.
    state.unset_setting("output.post_process.timeout_ms")
    assert "post_process" not in state.doc.get("output", {})


async def test_language_input_empty_triggers_unset(tmp_path, monkeypatch):
    """Integration check: Settings tab's language Input, when cleared,
    runs through unset_setting rather than silently ignoring. This is
    the user-visible symptom path — the `fr` survival bug."""
    import shutil
    import tomlkit as _tk
    from voxtype_tui import voxtype_cli
    from voxtype_tui.app import VoxtypeTUI
    from voxtype_tui.settings import SettingsPane
    from .conftest import FIXTURES
    from textual.widgets import Input

    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)
    # Seed the config with language="fr" to simulate the stuck state.
    doc = _tk.parse(cfg.read_text())
    if "whisper" not in doc:
        doc["whisper"] = _tk.table()
    doc["whisper"]["language"] = "fr"
    cfg.write_text(_tk.dumps(doc))

    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("3")  # Settings tab
        await pilot.pause()
        pane = app.query_one(SettingsPane)
        # Pre-check: state loaded with fr.
        assert str(app.state.doc["whisper"]["language"]) == "fr"
        # Clear the language Input; Textual fires Changed, our handler
        # routes through unset_setting, key disappears from doc.
        lang_input = pane.query_one("#settings-language", Input)
        lang_input.value = ""
        await pilot.pause()
        assert "language" not in app.state.doc.get("whisper", {})
        assert app.state.config_dirty is True


def test_placeholder_does_not_advertise_example_languages():
    """The original `"en, auto, de, fr, …"` placeholder led users to
    type `fr` etc. thinking the field accepted a preset. The fix is to
    avoid listing specific non-English codes in the placeholder. This
    test pins that regression: the placeholder string must not contain
    any two-letter language code other than `en`.
    """
    import re
    from pathlib import Path
    settings_src = (
        Path(__file__).resolve().parent.parent
        / "voxtype_tui" / "settings.py"
    ).read_text()
    # Find the settings-language Input placeholder.
    match = re.search(
        r'id="settings-language".*?placeholder="([^"]+)"',
        settings_src, flags=re.DOTALL,
    )
    if match is None:
        match = re.search(
            r'placeholder="([^"]+)".*?id="settings-language"',
            settings_src, flags=re.DOTALL,
        )
    assert match, "Could not locate settings-language placeholder in settings.py"
    placeholder = match.group(1)
    # The only two-letter code allowed in the placeholder is `en`.
    # Any of de/fr/es/it/ja/zh/ko/pt/nl/… would re-introduce the bug.
    BAD_CODES = ["de", "fr", "es", "it", "ja", "zh", "ko", "pt",
                 "nl", "ru", "pl", "sv", "tr", "auto"]
    for code in BAD_CODES:
        assert code not in placeholder.lower(), (
            f"Language placeholder advertises {code!r} as an option — "
            f"users type it verbatim and it sticks in their config. "
            f"Current placeholder: {placeholder!r}"
        )
