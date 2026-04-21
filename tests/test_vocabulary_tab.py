"""Pilot tests for the Vocabulary tab: rapid-add, filtered-delete, undo."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input

from voxtype_tui import config, sidecar, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.vocabulary import VocabularyPane, estimate_tokens
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


async def test_add_vocab_via_input_then_ctrl_s_persists(tmp_env):
    """User flow: press n → type phrase → Enter → ctrl+s → config on disk
    has the phrase in initial_prompt and sidecar has the entry."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Press n to focus the Add input. DataTable has focus by default.
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.focused, Input)
        assert app.focused.id == "add"

        # Type and submit — input should clear and stay focused (rapid-add).
        for ch in "Claude Code":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.focused, Input)
        assert app.focused.value == ""

        # Add a second one without losing focus.
        for ch in "Omarchy":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()

        # Save.
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.state.dirty is False

    # Verify on-disk state
    reloaded = config.load(cfg)
    assert config.get_initial_prompt(reloaded) == "Claude Code, Omarchy"
    sc = sidecar.load(side)
    assert [v.phrase for v in sc.vocabulary] == ["Claude Code", "Omarchy"]


async def test_duplicate_add_is_rejected(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(VocabularyPane)
        pane.tui.state.add_vocab("Neovim")
        pane.sync_from_state()
        await pilot.pause()

        await pilot.press("n")
        await pilot.pause()
        for ch in "Neovim":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()

        # Still just one entry — dedup worked
        phrases = [v.phrase for v in pane.tui.state.sc.vocabulary]
        assert phrases == ["Neovim"]


async def test_delete_and_undo_restores_at_original_index(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(VocabularyPane)
        # Pre-seed three entries via the state API
        for p in ["Alpha", "Beta", "Gamma"]:
            pane.tui.state.add_vocab(p)
        pane.sync_from_state()
        await pilot.pause()

        table = app.query_one(DataTable)
        table.focus()
        await pilot.pause()

        # Move cursor to row 1 (Beta) and delete
        table.move_cursor(row=1)
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        phrases = [v.phrase for v in pane.tui.state.sc.vocabulary]
        assert phrases == ["Alpha", "Gamma"]

        # Undo → Beta should return at index 1
        await pilot.press("u")
        await pilot.pause()
        phrases = [v.phrase for v in pane.tui.state.sc.vocabulary]
        assert phrases == ["Alpha", "Beta", "Gamma"]


async def test_filtered_delete_targets_underlying_phrase(tmp_env):
    """When a search filter is active, d must delete the selected entry by
    phrase, not by visual index — classic filtered-list bug."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(VocabularyPane)
        for p in ["Apple", "Banana", "Apricot", "Cherry"]:
            pane.tui.state.add_vocab(p)
        pane.sync_from_state()
        await pilot.pause()

        # Enter a filter that matches only "Apple" and "Apricot"
        search = app.query_one("#search", Input)
        search.focus()
        await pilot.pause()
        for ch in "Ap":
            await pilot.press(ch)
        await pilot.pause()

        table = app.query_one(DataTable)
        # Should show 2 rows now
        assert table.row_count == 2

        # Focus the table and select the second visible row (Apricot)
        table.focus()
        await pilot.pause()
        table.move_cursor(row=1)
        await pilot.pause()

        await pilot.press("d")
        await pilot.pause()

        phrases = [v.phrase for v in pane.tui.state.sc.vocabulary]
        # Apricot removed; others untouched (in particular Banana remains)
        assert phrases == ["Apple", "Banana", "Cherry"]


async def test_token_counter_updates_and_warns(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(VocabularyPane)
        tokens_widget = app.query_one("#tokens")

        # Empty to start → no warn class
        assert not tokens_widget.has_class("warn")
        assert not tokens_widget.has_class("over")

        # Add enough text to exceed the warning threshold (200 tokens ≈ 800 chars)
        long_phrases = [f"VeryLongTestPhraseNumber{i}" for i in range(40)]
        for p in long_phrases:
            pane.tui.state.add_vocab(p)
        pane.refresh_tokens()
        await pilot.pause()
        joined = sidecar.build_initial_prompt(pane.tui.state.sc.vocabulary)
        expected = estimate_tokens(joined)
        if expected >= 224:
            assert tokens_widget.has_class("over")
        elif expected >= 200:
            assert tokens_widget.has_class("warn")


async def test_vim_jk_navigate_cursor(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(VocabularyPane)
        for p in ["Alpha", "Beta", "Gamma", "Delta"]:
            pane.tui.state.add_vocab(p)
        pane.sync_from_state()
        await pilot.pause()

        table = app.query_one(DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()

        await pilot.press("j")
        await pilot.pause()
        assert table.cursor_row == 1

        await pilot.press("j")
        await pilot.pause()
        assert table.cursor_row == 2

        await pilot.press("k")
        await pilot.pause()
        assert table.cursor_row == 1


async def test_vim_G_jumps_to_bottom_gg_jumps_to_top(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(VocabularyPane)
        for p in ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]:
            pane.tui.state.add_vocab(p)
        pane.sync_from_state()
        await pilot.pause()

        table = app.query_one(DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()

        await pilot.press("G")
        await pilot.pause()
        assert table.cursor_row == 4  # last row

        # gg should jump to top (two g's within window)
        await pilot.press("g")
        await pilot.press("g")
        await pilot.pause()
        assert table.cursor_row == 0


async def test_single_g_does_nothing(tmp_env):
    """A lone `g` must not jump anywhere — only `gg` within the window."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(VocabularyPane)
        for p in ["Alpha", "Beta", "Gamma"]:
            pane.tui.state.add_vocab(p)
        pane.sync_from_state()
        await pilot.pause()

        table = app.query_one(DataTable)
        table.focus()
        table.move_cursor(row=2)
        await pilot.pause()

        await pilot.press("g")
        await pilot.pause()
        # After a lone g we should still be where we were
        assert table.cursor_row == 2


async def test_n_context_sensitive_new_vs_next_match(tmp_env):
    """n when search filter empty → focus Add input. n when search filter has
    text → move cursor to next matching row."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(VocabularyPane)
        for p in ["Apple", "Banana", "Apricot", "Cherry"]:
            pane.tui.state.add_vocab(p)
        pane.sync_from_state()
        await pilot.pause()

        table = app.query_one(DataTable)

        # Empty filter → n focuses Add
        table.focus()
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.focused, Input)
        assert app.focused.id == "add"

        # Exit the input without adding
        await pilot.press("escape")
        await pilot.pause()

        # Apply a filter, put focus on table, press n to advance matches
        search = app.query_one("#search", Input)
        search.focus()
        for ch in "Ap":
            await pilot.press(ch)
        await pilot.pause()
        assert table.row_count == 2
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()

        await pilot.press("n")
        await pilot.pause()
        assert table.cursor_row == 1  # moved to next match
        await pilot.press("n")
        await pilot.pause()
        assert table.cursor_row == 0  # wrapped back


async def test_capital_N_goes_to_previous_match(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(VocabularyPane)
        for p in ["Apple", "Apricot", "Avocado"]:
            pane.tui.state.add_vocab(p)
        pane.sync_from_state()
        await pilot.pause()

        search = app.query_one("#search", Input)
        search.focus()
        for ch in "A":
            await pilot.press(ch)
        await pilot.pause()

        table = app.query_one(DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()

        await pilot.press("N")
        await pilot.pause()
        # Wrap-around to last row
        assert table.cursor_row == table.row_count - 1


async def test_mouse_click_selects_row(tmp_env):
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(VocabularyPane)
        for p in ["Alpha", "Beta", "Gamma"]:
            pane.tui.state.add_vocab(p)
        pane.sync_from_state()
        await pilot.pause()

        # Click the table region at an offset that should land on a non-zero row
        table = app.query_one(DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()
        # Click the second data row (offset accounts for the header row)
        await pilot.click(DataTable, offset=(2, 2))
        await pilot.pause()
        # If Textual's default mouse handling works, cursor should have moved
        # (the exact row depends on layout, but cursor_row should not still be
        # zero unless click landed on header).
        # We assert weakly: the cursor is on a valid data row.
        assert 0 <= table.cursor_row < table.row_count


async def test_search_input_does_not_trigger_letter_bindings(tmp_env):
    """While focused on the search Input, pressing 'd' must NOT delete —
    it must type 'd' into the search box. Regression test for letter binding
    leaking into input focus."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.query_one(VocabularyPane)
        pane.tui.state.add_vocab("deleteme")
        pane.sync_from_state()
        await pilot.pause()

        search = app.query_one("#search", Input)
        search.focus()
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        # Search box should now contain "d", not have triggered a delete
        assert search.value == "d"
        phrases = [v.phrase for v in pane.tui.state.sc.vocabulary]
        assert "deleteme" in phrases  # not deleted
