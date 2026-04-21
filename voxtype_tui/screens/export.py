"""Manual-export modal (Ctrl+E).

Fires a policy-driven `write_export_bundle` behind the scenes. The policy
layer (scope + redact) lives in `voxtype_tui.sync` so it's unit-testable
without a Pilot harness; this screen is the thin UI strip on top.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, RadioButton, RadioSet

from .. import sync
from ..theme import MODAL_BORDER_STYLE

if TYPE_CHECKING:
    from ..state import AppState


class ExportBundleModal(ModalScreen[Path | None]):
    """Prompt the user for export target + policy knobs, then write.

    Dismissal contract:
      * Success → dismiss with the absolute `Path` that was written.
      * Cancel / failed write → dismiss with `None`. Failures surface as
        an in-modal error `Label`; the user can adjust the path and
        retry without reopening the dialog.
    """

    DEFAULT_CSS = f"""
    ExportBundleModal {{ align: center middle; }}
    ExportBundleModal > Vertical {{
        background: $panel;
        border: {MODAL_BORDER_STYLE} $accent;
        padding: 1 2;
        width: 78;
        height: auto;
    }}
    ExportBundleModal #title {{ text-style: bold; margin-bottom: 1; }}
    ExportBundleModal .field {{ height: auto; margin-bottom: 1; }}
    ExportBundleModal .field > Label {{ color: $text-muted; width: 18; }}
    ExportBundleModal #export-path {{ width: 1fr; }}
    ExportBundleModal RadioSet {{ width: 1fr; height: auto; }}
    ExportBundleModal #redact-row {{ height: auto; margin-bottom: 1; }}
    ExportBundleModal #redact-row > Label {{ color: $text-muted; width: 18; }}
    ExportBundleModal #redact-row > #redact-hint {{
        color: $text-muted; padding-left: 1; width: 1fr;
    }}
    ExportBundleModal #error {{ color: $error; margin-bottom: 1; }}
    ExportBundleModal #error.hidden {{ display: none; }}
    ExportBundleModal Horizontal#actions {{
        height: auto; align: center middle;
    }}
    ExportBundleModal Button {{ margin: 0 1; }}
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, state: "AppState") -> None:
        super().__init__()
        self.state = state
        self._secrets_present = bool(sync.distill_secrets(state.doc))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Export bundle", id="title")

            with Horizontal(classes="field"):
                yield Label("Target path", classes="label")
                yield Input(
                    value=str(sync.default_export_path()),
                    id="export-path",
                )

            with Horizontal(classes="field"):
                yield Label("Scope", classes="label")
                with RadioSet(id="export-scope"):
                    yield RadioButton("Sync only", id="scope-sync", value=True)
                    yield RadioButton("Sync + Local", id="scope-sync-local")

            with Horizontal(id="redact-row"):
                yield Label("Secrets", classes="label")
                yield Checkbox(
                    "Redact",
                    value=True,
                    id="export-redact",
                    # Disabled when there are no secrets in the current
                    # config — nothing to redact, toggle is meaningless.
                    disabled=not self._secrets_present,
                )
                yield Label(
                    "blank remote_api_key and *_command fields"
                    if self._secrets_present
                    else "(no secrets configured)",
                    id="redact-hint",
                )

            yield Label("", id="error", classes="hidden")

            with Horizontal(id="actions"):
                yield Button("Export", variant="primary", id="do-export")
                yield Button("Cancel", variant="default", id="do-cancel")

    # --- actions ---

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "do-cancel":
            event.stop()
            self.dismiss(None)
        elif event.button.id == "do-export":
            event.stop()
            self._do_export()

    def _do_export(self) -> None:
        path_input = self.query_one("#export-path", Input)
        target_str = path_input.value.strip()
        if not target_str:
            self._show_error("Target path is required")
            return

        scope_set = self.query_one("#export-scope", RadioSet)
        pressed = scope_set.pressed_button
        scope = (
            sync.SCOPE_SYNC_ONLY
            if pressed is None or pressed.id == "scope-sync"
            else sync.SCOPE_SYNC_PLUS_LOCAL
        )

        redact_widget = self.query_one("#export-redact", Checkbox)
        # `disabled` checkboxes still carry a `value`; treat absence of
        # secrets as "nothing to redact" — the emitted bundle has an
        # empty secrets block either way.
        redact = bool(redact_widget.value) if self._secrets_present else True

        try:
            bundle = sync.build_export_bundle(
                self.state.doc,
                self.state.sc,
                scope=scope,
                redact_secrets=redact,
            )
            written = sync.write_export_bundle(bundle, Path(target_str))
        except (OSError, sync.BundleError, ValueError) as e:
            # Present the failure in-modal so the user can adjust the
            # path (permission denied / missing parent / …) without
            # reopening the dialog.
            self._show_error(str(e))
            return

        self.dismiss(written)

    def _show_error(self, msg: str) -> None:
        err = self.query_one("#error", Label)
        err.update(f"⚠ {msg}")
        err.remove_class("hidden")
