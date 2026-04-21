"""Dictionary tab — post-transcription text replacements.

Voxtype stores these as one flat `[text].replacements` map on disk. The TUI
decorates each entry with a category (Replacement / Command / Capitalization)
kept in the sidecar. Category changes are sidecar-only; adding/editing
from→to updates both.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Select, Static

from . import sidecar

if TYPE_CHECKING:
    from .app import VoxtypeTUI

GG_SEQUENCE_WINDOW = 0.5


@dataclass
class UndoEntry:
    from_text: str
    to_text: str
    category: str
    index: int


class DictionaryPane(Vertical):
    DEFAULT_CSS = """
    DictionaryPane { padding: 1 2; }
    DictionaryPane #dict-add-row { height: 3; margin-bottom: 1; }
    DictionaryPane #dict-add-from { width: 1fr; }
    DictionaryPane #dict-add-to { width: 1fr; margin-left: 1; }
    DictionaryPane #dict-category { width: 22; margin-left: 1; }
    DictionaryPane #dict-search { margin-bottom: 1; }
    DictionaryPane DataTable { height: 1fr; }
    DictionaryPane #dict-hints { height: 1; color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("n", "vim_n", "Add / next match"),
        Binding("N", "vim_prev_match", "Prev match", show=False),
        Binding("/", "focus_search", "Search"),
        Binding("d", "delete_selected", "Delete"),
        Binding("u", "undo_delete", "Undo"),
        Binding("c", "cycle_category", "Cycle category"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "vim_g", "Top (gg)", show=False),
        Binding("G", "vim_bottom", "Bottom", show=False),
        Binding("escape", "focus_table", "Back to list", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.undo_stack: list[UndoEntry] = []
        self._last_g_time: float = 0.0

    def compose(self) -> ComposeResult:
        with Horizontal(id="dict-add-row"):
            yield Input(placeholder="From (what you say)…", id="dict-add-from")
            yield Input(placeholder="To (what appears)…", id="dict-add-to")
            yield Select(
                options=[(c, c) for c in sidecar.CATEGORIES],
                prompt="Category",
                value=sidecar.DEFAULT_CATEGORY,
                allow_blank=False,
                id="dict-category",
            )
        yield Input(placeholder="Search from or to (press / to focus)", id="dict-search")
        yield DataTable(id="dict-table", cursor_type="row", zebra_stripes=True)
        yield Static(
            "n add/next  ·  N prev  ·  / search  ·  d delete  ·  "
            "u undo  ·  c cycle category  ·  j/k gg/G nav  ·  ctrl+s save",
            id="dict-hints",
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("From", "To", "Category")
        self.refresh_table()

    # --- check_action ---

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        disabled_on_input = {
            "focus_search", "delete_selected", "undo_delete",
            "vim_n", "vim_prev_match", "cycle_category",
            "cursor_down", "cursor_up", "vim_g", "vim_bottom",
        }
        if action in disabled_on_input and isinstance(self.app.focused, Input):
            return False
        if action == "focus_table":
            if not isinstance(self.app.focused, Input):
                return False
        return True

    def _filter_active(self) -> bool:
        return bool(self.query_one("#dict-search", Input).value.strip())

    # --- helpers ---

    @property
    def tui(self) -> "VoxtypeTUI":
        return self.app  # type: ignore[return-value]

    def _entries(self) -> list[sidecar.ReplacementEntry]:
        if self.tui.state is None:
            return []
        return self.tui.state.sc.replacements

    def _to_text(self, from_text: str) -> str:
        if self.tui.state is None:
            return ""
        from . import config
        return config.get_replacements(self.tui.state.doc).get(from_text, "")

    def refresh_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        q = self.query_one("#dict-search", Input).value.strip().lower()
        for r in self._entries():
            to_text = self._to_text(r.from_text)
            if q and q not in r.from_text.lower() and q not in to_text.lower():
                continue
            table.add_row(r.from_text, to_text, r.category, key=r.from_text)

    def _selected_from_text(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(
                (table.cursor_row, 0)
            ).row_key
        except Exception:
            return None
        return row_key.value

    # --- actions ---

    def action_focus_search(self) -> None:
        self.query_one("#dict-search", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one(DataTable).focus()

    def action_focus_add(self) -> None:
        self.query_one("#dict-add-from", Input).focus()

    def action_vim_n(self) -> None:
        if self._filter_active():
            self._move_cursor_wrap(+1)
        else:
            self.action_focus_add()

    def action_vim_prev_match(self) -> None:
        self._move_cursor_wrap(-1)

    def action_cursor_down(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count > 0:
            table.action_cursor_down()

    def action_cursor_up(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count > 0:
            table.action_cursor_up()

    def action_vim_g(self) -> None:
        now = time.monotonic()
        if now - self._last_g_time < GG_SEQUENCE_WINDOW:
            table = self.query_one(DataTable)
            if table.row_count > 0:
                table.move_cursor(row=0)
            self._last_g_time = 0.0
        else:
            self._last_g_time = now

    def action_vim_bottom(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count > 0:
            table.move_cursor(row=table.row_count - 1)

    def _move_cursor_wrap(self, delta: int) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        target = (table.cursor_row + delta) % table.row_count
        table.move_cursor(row=target)

    def action_cycle_category(self) -> None:
        if self.tui.state is None:
            return
        from_text = self._selected_from_text()
        if from_text is None:
            return
        new_cat = self.tui.state.cycle_replacement_category(from_text)
        if new_cat is None:
            return
        self.refresh_table()
        self.tui.refresh_dirty()
        self.app.notify(f"'{from_text}' → {new_cat}", timeout=2)

    def action_delete_selected(self) -> None:
        if self.tui.state is None:
            return
        from_text = self._selected_from_text()
        if from_text is None:
            return
        to_text = self._to_text(from_text)
        entries = self._entries()
        try:
            idx = next(i for i, r in enumerate(entries) if r.from_text == from_text)
        except StopIteration:
            return
        category = entries[idx].category
        was_last = len(entries) == 1

        if not self.tui.state.remove_replacement(from_text):
            return
        self.undo_stack.append(UndoEntry(
            from_text=from_text, to_text=to_text,
            category=category, index=idx,
        ))
        self.refresh_table()
        self.tui.refresh_dirty()
        if was_last:
            self.app.notify(
                "Cleared last replacement. The [text.replacements] section "
                "and any inline comments in it will be removed on save.",
                severity="warning",
                timeout=6,
                title="Heads up",
            )
        else:
            self.app.notify(
                f"Deleted '{from_text}' — press u to undo",
                timeout=4,
            )

    def action_undo_delete(self) -> None:
        if not self.undo_stack or self.tui.state is None:
            return
        entry = self.undo_stack.pop()
        self.tui.state.upsert_replacement(
            entry.from_text, entry.to_text, entry.category
        )
        self.refresh_table()
        self.tui.refresh_dirty()
        self.app.notify(f"Restored '{entry.from_text}'", timeout=2)

    # --- input events ---

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in either from-input or to-input triggers add.
        if event.input.id not in ("dict-add-from", "dict-add-to"):
            return
        event.stop()
        from_input = self.query_one("#dict-add-from", Input)
        to_input = self.query_one("#dict-add-to", Input)
        cat_select = self.query_one("#dict-category", Select)

        from_text = from_input.value.strip()
        to_text = to_input.value.strip()
        category = cat_select.value if isinstance(cat_select.value, str) else sidecar.DEFAULT_CATEGORY

        if not from_text or not to_text:
            self.app.notify("Both 'From' and 'To' are required", severity="warning", timeout=3)
            return
        if self.tui.state is None:
            return
        self.tui.state.upsert_replacement(from_text, to_text, category)
        from_input.value = ""
        to_input.value = ""
        from_input.focus()
        self.refresh_table()
        self.tui.refresh_dirty()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "dict-search":
            self.refresh_table()

    # --- external hook ---

    def sync_from_state(self) -> None:
        self.undo_stack.clear()
        self.refresh_table()
