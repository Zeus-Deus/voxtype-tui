"""Pilot tests for the Dictionary tab: add form, category cycle, delete+undo,
filtered nav, mouse."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Select

from voxtype_tui import config, sidecar, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.dictionary import DictionaryPane
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


async def _goto_dictionary(pilot, app):
    await pilot.press("2")
    await pilot.pause()
    assert app.query_one("#tabs").active == "dictionary"


async def test_add_replacement_persists_with_category(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_dictionary(pilot, app)
        pane = app.query_one(DictionaryPane)

        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.focused, Input)
        assert app.focused.id == "from-input"

        for ch in "vox type":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("tab")
        await pilot.pause()
        for ch in "voxtype":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()

        assert pane.query_one("#from-input", Input).value == ""
        assert pane.query_one("#to-input", Input).value == ""

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.state.dirty is False

    reloaded = config.load(cfg)
    assert config.get_replacements(reloaded) == {"vox type": "voxtype"}
    sc = sidecar.load(side)
    assert [(r.from_text, r.category) for r in sc.replacements] == [
        ("vox type", "Replacement")
    ]


async def test_cycle_category_only_marks_sidecar_dirty(tmp_env):
    """Changing a row's category via `c` must NOT flip config_dirty — the
    category lives only in the sidecar. Restart-sensitive diff must stay
    empty."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_dictionary(pilot, app)

        app.state.upsert_replacement("vox type", "voxtype", "Replacement")
        app.refresh_dirty()
        await pilot.pause()
        # Save so we start from a clean slate
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not app.state.dirty

        pane = app.query_one(DictionaryPane)
        pane.sync_from_state()
        await pilot.pause()
        table = pane.query_one(DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()

        await pilot.press("c")
        await pilot.pause()

        assert app.state.config_dirty is False, (
            "category cycle must not dirty config.toml"
        )
        assert app.state.sidecar_dirty is True
        # Category should have advanced to the next one
        cats = [r.category for r in app.state.sc.replacements]
        assert cats == ["Command"]


async def test_cycle_category_wraps(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_dictionary(pilot, app)

        app.state.upsert_replacement("vox type", "voxtype", "Capitalization")
        pane = app.query_one(DictionaryPane)
        pane.sync_from_state()
        await pilot.pause()
        table = pane.query_one(DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()

        await pilot.press("c")  # Capitalization → wraps to Replacement
        await pilot.pause()
        assert app.state.sc.replacements[0].category == "Replacement"


async def test_delete_and_undo_restores_category(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_dictionary(pilot, app)

        app.state.upsert_replacement("vox type", "voxtype", "Replacement")
        app.state.upsert_replacement("slash deploy", "/deploy", "Command")
        app.state.upsert_replacement("kube ctl", "kubectl", "Capitalization")
        pane = app.query_one(DictionaryPane)
        pane.sync_from_state()
        await pilot.pause()

        table = pane.query_one(DataTable)
        table.focus()
        table.move_cursor(row=1)  # slash deploy
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        from_texts = [r.from_text for r in app.state.sc.replacements]
        assert from_texts == ["vox type", "kube ctl"]

        await pilot.press("u")
        await pilot.pause()
        entries = [(r.from_text, r.category) for r in app.state.sc.replacements]
        # Order after undo puts it back; category preserved
        assert ("slash deploy", "Command") in entries


async def test_delete_last_replacement_shows_section_warning_toast(tmp_env):
    """When you delete the last replacement, a warning toast is shown about
    the [text.replacements] section being removed on save."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_dictionary(pilot, app)

        app.state.upsert_replacement("only one", "lone", "Replacement")
        pane = app.query_one(DictionaryPane)
        pane.sync_from_state()
        await pilot.pause()

        table = pane.query_one(DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()

        # Capture notifications
        notifications: list = []
        orig = app.notify
        def capture(msg, **kwargs):
            notifications.append((str(msg), kwargs))
            return orig(msg, **kwargs)
        app.notify = capture  # type: ignore[assignment]

        await pilot.press("d")
        await pilot.pause()

        assert app.state.sc.replacements == []
        assert any(
            "[text.replacements]" in m and kw.get("severity") == "warning"
            for m, kw in notifications
        ), f"expected a warning toast, got {notifications}"


async def test_filter_searches_both_from_and_to(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_dictionary(pilot, app)

        app.state.upsert_replacement("vox type", "voxtype", "Replacement")
        app.state.upsert_replacement("cap s", "CapitalS", "Capitalization")
        app.state.upsert_replacement("kube", "kubectl", "Replacement")
        pane = app.query_one(DictionaryPane)
        pane.sync_from_state()
        await pilot.pause()

        search = pane.query_one("#search", Input)
        search.focus()
        for ch in "kube":
            await pilot.press(ch)
        await pilot.pause()

        table = pane.query_one(DataTable)
        assert table.row_count == 1  # matches via from-text

        search.value = ""
        await pilot.pause()
        for ch in "Capital":
            await pilot.press(ch)
        await pilot.pause()
        assert table.row_count == 1  # matches via to-text


async def test_add_requires_both_fields(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_dictionary(pilot, app)

        pane = app.query_one(DictionaryPane)
        from_input = pane.query_one("#from-input", Input)
        from_input.focus()
        await pilot.pause()
        for ch in "solo":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()

        # No replacement added
        assert app.state.sc.replacements == []


async def test_vim_n_on_dictionary_focuses_from_input(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _goto_dictionary(pilot, app)

        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.focused, Input)
        assert app.focused.id == "from-input"
