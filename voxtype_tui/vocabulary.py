"""Vocabulary tab — list of phrases that get injected into Whisper's
initial_prompt. Managed as structured entries in the sidecar; serialized to a
comma-joined string on save.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Static

from . import sidecar
from .vim_nav import VIM_NAV_ACTIONS, VIM_NAV_BINDINGS, VimTableNav

if TYPE_CHECKING:
    from .app import VoxtypeTUI


TOKEN_LIMIT = 224
TOKEN_WARN = 200


def estimate_tokens(text: str) -> int:
    """Conservative rough estimate: chars/4. Whisper uses BPE so the real
    count varies, but chars/4 is a reasonable upper bound for English proper
    nouns with some multi-byte characters."""
    return (len(text) + 3) // 4


@dataclass
class UndoEntry:
    phrase: str
    index: int


class VocabularyPane(VimTableNav, Vertical):
    DEFAULT_CSS = """
    VocabularyPane { padding: 1 2; }
    VocabularyPane #vocab-top { height: 3; margin-bottom: 1; }
    VocabularyPane #vocab-add { width: 1fr; }
    VocabularyPane #vocab-tokens {
        width: 24;
        height: 3;
        padding: 1;
        margin-left: 1;
        background: $boost;
        content-align: center middle;
    }
    VocabularyPane #vocab-tokens.warn { background: $warning 30%; color: $warning; }
    VocabularyPane #vocab-tokens.over  { background: $error 30%; color: $error; }
    VocabularyPane #vocab-search { margin-bottom: 1; }
    VocabularyPane DataTable { height: 1fr; }
    VocabularyPane #vocab-hints {
        height: 1;
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("n", "vim_n", "Add / next match"),
        Binding("/", "focus_search", "Search"),
        Binding("d", "delete_selected", "Delete"),
        Binding("u", "undo_delete", "Undo"),
        Binding("escape", "focus_table", "Back to list", show=False),
    ] + VIM_NAV_BINDINGS

    def __init__(self) -> None:
        super().__init__()
        self.undo_stack: list[UndoEntry] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="vocab-top"):
            yield Input(
                placeholder="Add phrase and press Enter — rapid-add keeps focus here…",
                id="vocab-add",
            )
            yield Static(id="vocab-tokens")
        yield Input(placeholder="Search (press / to focus)", id="vocab-search")
        yield DataTable(id="vocab-table", cursor_type="row", zebra_stripes=True)
        yield Static(
            "n add/next  ·  N prev  ·  / search  ·  d delete  ·  "
            "u undo  ·  j/k gg/G nav  ·  ctrl+s save",
            id="vocab-hints",
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Phrase", "Added")
        self.refresh_table()
        self.refresh_tokens()

    # ---- check_action: disable letter bindings while an Input has focus ----

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Every single-letter binding must yield to Input focus — otherwise
        # typing a phrase containing these letters would trigger navigation
        # instead of inserting the character.
        pane_actions = {"focus_search", "delete_selected", "undo_delete", "vim_n"}
        if action in pane_actions | VIM_NAV_ACTIONS and isinstance(self.app.focused, Input):
            return False
        if action == "focus_table":
            if not isinstance(self.app.focused, Input):
                return False
        return True

    def _filter_active(self) -> bool:
        return bool(self.query_one("#vocab-search", Input).value.strip())

    # ---- view refresh ----

    @property
    def tui(self) -> "VoxtypeTUI":
        return self.app  # type: ignore[return-value]

    def _entries(self) -> list[sidecar.VocabEntry]:
        if self.tui.state is None:
            return []
        return self.tui.state.sc.vocabulary

    def refresh_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        q = self.query_one("#vocab-search", Input).value.strip().lower()
        for v in self._entries():
            if q and q not in v.phrase.lower():
                continue
            added = v.added_at[:10] if v.added_at else ""
            table.add_row(v.phrase, added, key=v.phrase)

    def refresh_tokens(self) -> None:
        text = sidecar.build_initial_prompt(self._entries())
        count = estimate_tokens(text)
        widget = self.query_one("#vocab-tokens", Static)
        widget.update(f"~{count} / {TOKEN_LIMIT} tok")
        widget.remove_class("warn")
        widget.remove_class("over")
        if count >= TOKEN_LIMIT:
            widget.add_class("over")
        elif count >= TOKEN_WARN:
            widget.add_class("warn")

    # ---- actions ----

    def action_focus_add(self) -> None:
        self.query_one("#vocab-add", Input).focus()

    def action_focus_search(self) -> None:
        self.query_one("#vocab-search", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one(DataTable).focus()

    def action_vim_n(self) -> None:
        """`n` is context-sensitive: cycle next match when a search filter is
        active, otherwise focus the Add input."""
        if self._filter_active():
            self._move_cursor_wrap(+1)
        else:
            self.action_focus_add()

    def action_delete_selected(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        try:
            row_key = table.coordinate_to_cell_key(
                (table.cursor_row, 0)
            ).row_key
        except Exception:
            return
        phrase = row_key.value
        if phrase is None:
            return
        entries = [v.phrase for v in self._entries()]
        try:
            idx = entries.index(phrase)
        except ValueError:
            return
        if not self.tui.state:
            return
        if self.tui.state.remove_vocab(phrase):
            self.undo_stack.append(UndoEntry(phrase=phrase, index=idx))
            self.refresh_table()
            self.refresh_tokens()
            self.tui.refresh_dirty()
            self.app.notify(
                f"Deleted '{phrase}' — press u to undo",
                timeout=4,
            )

    def action_undo_delete(self) -> None:
        if not self.undo_stack or not self.tui.state:
            return
        entry = self.undo_stack.pop()
        current = [v.phrase for v in self._entries()]
        current.insert(min(entry.index, len(current)), entry.phrase)
        self.tui.state.set_vocabulary(current)
        self.refresh_table()
        self.refresh_tokens()
        self.tui.refresh_dirty()
        self.app.notify(f"Restored '{entry.phrase}'", timeout=2)

    # ---- input events ----

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "vocab-add":
            return
        phrase = event.value.strip()
        event.stop()
        if not phrase:
            return
        if not self.tui.state:
            return
        if self.tui.state.add_vocab(phrase):
            event.input.value = ""
            self.refresh_table()
            self.refresh_tokens()
            self.tui.refresh_dirty()
        else:
            self.app.notify(
                f"'{phrase}' is already in the vocabulary",
                severity="warning",
                timeout=3,
            )
            event.input.value = ""

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "vocab-search":
            self.refresh_table()

    # ---- external hook: called from the app when state reloads ----

    def sync_from_state(self) -> None:
        self.undo_stack.clear()
        self.refresh_table()
        self.refresh_tokens()
