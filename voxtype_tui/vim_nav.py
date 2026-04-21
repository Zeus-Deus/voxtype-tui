"""Vim-style navigation for panes built around a DataTable.

Factored out so Vocabulary, Dictionary, and any future table-heavy tab
(Models) get the same j/k/gg/G/N/_move_cursor_wrap behavior without copying
a hundred lines of boilerplate.

Host class contract:
  1. Subclass VimTableNav first: `class Foo(VimTableNav, Vertical)`.
  2. Concatenate `VIM_NAV_BINDINGS` into the pane's own BINDINGS list —
     Textual's `_merge_bindings` skips classes that aren't DOMNode subclasses,
     so a bare mixin-level BINDINGS is silently ignored. Hence the explicit
     list-concat pattern.
  3. Contain a DataTable queryable via `self.query_one(DataTable)`.
  4. Include `VIM_NAV_ACTIONS` in the set of actions disabled while an Input
     widget has focus (inside `check_action`). Otherwise typing a phrase
     containing 'j', 'k', 'g', 'G', or 'N' would move the cursor instead of
     inserting the character.

For `n` — the context-sensitive 'add-vs-next-match' key — the pane still owns
that binding; call `self._move_cursor_wrap(+1)` from its `action_vim_n`.
"""
from __future__ import annotations

import time

from textual.binding import Binding
from textual.widgets import DataTable

GG_SEQUENCE_WINDOW = 0.5

VIM_NAV_ACTIONS: frozenset[str] = frozenset({
    "cursor_down",
    "cursor_up",
    "vim_g",
    "vim_bottom",
    "vim_prev_match",
})


VIM_NAV_BINDINGS: list[Binding] = [
    Binding("j", "cursor_down", "Down", show=False),
    Binding("k", "cursor_up", "Up", show=False),
    Binding("g", "vim_g", "Top (gg)", show=False),
    Binding("G", "vim_bottom", "Bottom", show=False),
    Binding("N", "vim_prev_match", "Prev match", show=False),
]


class VimTableNav:
    _last_g_time: float = 0.0

    def _vim_table(self) -> DataTable | None:
        try:
            return self.query_one(DataTable)  # type: ignore[attr-defined]
        except Exception:
            return None

    def action_cursor_down(self) -> None:
        t = self._vim_table()
        if t is not None and t.row_count > 0:
            t.action_cursor_down()

    def action_cursor_up(self) -> None:
        t = self._vim_table()
        if t is not None and t.row_count > 0:
            t.action_cursor_up()

    def action_vim_g(self) -> None:
        now = time.monotonic()
        if now - self._last_g_time < GG_SEQUENCE_WINDOW:
            t = self._vim_table()
            if t is not None and t.row_count > 0:
                t.move_cursor(row=0)
            self._last_g_time = 0.0
        else:
            self._last_g_time = now

    def action_vim_bottom(self) -> None:
        t = self._vim_table()
        if t is not None and t.row_count > 0:
            t.move_cursor(row=t.row_count - 1)

    def action_vim_prev_match(self) -> None:
        self._move_cursor_wrap(-1)

    def _move_cursor_wrap(self, delta: int) -> None:
        t = self._vim_table()
        if t is None or t.row_count == 0:
            return
        target = (t.cursor_row + delta) % t.row_count
        t.move_cursor(row=target)
