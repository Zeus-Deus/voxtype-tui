"""Manual-import modal (Ctrl+I).

Two-stage flow in a single screen:

  1. File picker — user types a path, hits Load.
  2. Preview — full diff rendered; Apply merges into the in-memory
     state, Cancel backs out cleanly. User then hits Ctrl+S manually
     to persist (import itself never saves).

Policy / business logic lives in `voxtype_tui.sync`
(`load_bundle_file`, `diff_bundle_against_state`, `apply_bundle_to_state`);
this module is only UI plumbing.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from .. import sync
from ..theme import MODAL_BORDER_STYLE

if TYPE_CHECKING:
    from ..state import AppState


class ImportBundleModal(ModalScreen[bool]):
    """Dismiss outcome: True if a bundle was applied, False otherwise."""

    DEFAULT_CSS = f"""
    ImportBundleModal {{ align: center middle; }}
    ImportBundleModal > Vertical {{
        background: $panel;
        border: {MODAL_BORDER_STYLE} $accent;
        padding: 1 2;
        width: 90;
        height: 80%;
    }}
    ImportBundleModal #title {{ text-style: bold; margin-bottom: 1; }}
    ImportBundleModal .field {{ height: auto; margin-bottom: 1; }}
    ImportBundleModal .field > Label {{ color: $text-muted; width: 18; }}
    ImportBundleModal #import-path {{ width: 1fr; }}
    ImportBundleModal #import-load-row {{ height: auto; margin-bottom: 1; align: right middle; }}
    ImportBundleModal #import-load {{ margin-left: 1; }}
    ImportBundleModal #error {{ color: $error; margin-bottom: 1; }}
    ImportBundleModal #error.hidden {{ display: none; }}
    ImportBundleModal #preview-scroll {{
        height: 1fr;
        margin-bottom: 1;
        border: solid $primary-darken-2;
        padding: 0 1;
    }}
    ImportBundleModal #preview-scroll.hidden {{ display: none; }}
    ImportBundleModal .section-heading {{
        text-style: bold;
        margin-top: 1;
        color: $text;
    }}
    ImportBundleModal .danger {{
        color: $warning;
        text-style: bold;
    }}
    ImportBundleModal #include-local-row {{ height: auto; margin-bottom: 1; }}
    ImportBundleModal #include-local-row.hidden {{ display: none; }}
    ImportBundleModal #actions {{ height: auto; align: center middle; }}
    ImportBundleModal Button {{ margin: 0 1; }}
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, state: "AppState") -> None:
        super().__init__()
        self.state = state
        self._loaded_bundle: sync.Bundle | None = None
        self._load_warnings: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Import bundle", id="title")

            with Horizontal(classes="field"):
                yield Label("Source path", classes="label")
                yield Input(
                    placeholder="/path/to/voxtype-tui-export.json",
                    id="import-path",
                )

            with Horizontal(id="import-load-row"):
                yield Button("Load", variant="primary", id="do-load")

            with Horizontal(id="include-local-row", classes="hidden"):
                yield Checkbox(
                    "Import local settings (hotkey, audio device, VAD)",
                    value=False,
                    id="include-local",
                )

            yield Label("", id="error", classes="hidden")

            with VerticalScroll(id="preview-scroll", classes="hidden"):
                yield Static("", id="preview-body")

            with Horizontal(id="actions"):
                yield Button("Apply", variant="primary", id="do-apply", disabled=True)
                yield Button("Cancel", variant="default", id="do-cancel")

    # --- actions ---

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "do-cancel":
            event.stop()
            self.dismiss(False)
        elif bid == "do-load":
            event.stop()
            self._do_load()
        elif bid == "do-apply":
            event.stop()
            self._do_apply()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        # Recompute preview when the include-local toggle flips so the
        # diff honestly reflects what Apply will do.
        if event.checkbox.id == "include-local" and self._loaded_bundle is not None:
            self._refresh_preview()

    # --- load stage ---

    def _do_load(self) -> None:
        path_input = self.query_one("#import-path", Input)
        raw = path_input.value.strip()
        if not raw:
            self._show_error("Source path is required")
            return
        path = Path(raw).expanduser()
        try:
            bundle, warnings = sync.load_bundle_file(path)
        except sync.BundleError as e:
            self._loaded_bundle = None
            self._show_error(str(e))
            self.query_one("#preview-scroll").add_class("hidden")
            self.query_one("#do-apply", Button).disabled = True
            return

        self._clear_error()
        self._loaded_bundle = bundle
        self._load_warnings = list(warnings)

        # Show the local-import toggle only when the bundle actually
        # carries local data worth importing (voxtype-tui native bundles
        # may; Vexis never does).
        if bundle.local:
            self.query_one("#include-local-row").remove_class("hidden")
        else:
            self.query_one("#include-local-row").add_class("hidden")

        self._refresh_preview()
        self.query_one("#do-apply", Button).disabled = False

    def _refresh_preview(self) -> None:
        if self._loaded_bundle is None:
            return
        include_local = bool(
            self.query_one("#include-local", Checkbox).value
            if self._loaded_bundle.local else False
        )
        preview = sync.diff_bundle_against_state(
            self._loaded_bundle, self.state.doc, self.state.sc,
            include_local=include_local,
        )
        body = self.query_one("#preview-body", Static)
        body.update(_render_preview(preview, self._load_warnings))
        self.query_one("#preview-scroll").remove_class("hidden")

    # --- apply stage ---

    def _do_apply(self) -> None:
        if self._loaded_bundle is None:
            self._show_error("Nothing loaded yet")
            return
        include_local = bool(
            self.query_one("#include-local", Checkbox).value
            if self._loaded_bundle.local else False
        )
        warnings = sync.apply_bundle_to_state(
            self._loaded_bundle, self.state.doc, self.state.sc,
            include_local=include_local,
        )
        # Import writes to the in-memory shadow only. Flip dirty flags
        # so the top-bar pill and the save path recognize the pending
        # changes. The user must still Ctrl+S to persist — the import
        # modal intentionally never saves on the user's behalf.
        self.state.config_dirty = True
        self.state.sidecar_dirty = True
        for w in warnings:
            self.app.notify(w, severity="warning", timeout=8)
        self.dismiss(True)

    # --- error helpers ---

    def _show_error(self, msg: str) -> None:
        err = self.query_one("#error", Label)
        err.update(f"⚠ {msg}")
        err.remove_class("hidden")

    def _clear_error(self) -> None:
        err = self.query_one("#error", Label)
        err.update("")
        err.add_class("hidden")


def _render_preview(
    preview: sync.ImportPreview,
    load_warnings: list[str],
) -> str:
    """Human-readable summary of the preview diff. Plain text (no
    markup) so the Static widget renders deterministically across
    themes; the CSS class `danger` can't apply to inline spans here,
    so dangerous fields are prefixed with `⚠` to stand out."""
    lines: list[str] = []
    lines.append(f"Source: {preview.source}")
    for w in load_warnings:
        lines.append(f"· {w}")
    lines.append("")

    # Vocabulary
    v = preview.vocab
    lines.append(
        f"Vocabulary: {len(v.added)} new, {len(v.unchanged)} already present"
    )
    lines.extend(_bullet_list(v.added, "  + "))

    # Replacements
    r = preview.replacements
    lines.append("")
    lines.append(
        f"Replacements: {len(r.added)} new, {len(r.updated)} updated, "
        f"{len(r.unchanged)} unchanged"
    )
    for frm, to in r.added[:10]:
        lines.append(f"  + {frm!r} → {to!r}")
    if len(r.added) > 10:
        lines.append(f"  … and {len(r.added) - 10} more")
    for frm, old, new in r.updated[:10]:
        lines.append(f"  ~ {frm!r}: {old!r} → {new!r}")
    if len(r.updated) > 10:
        lines.append(f"  … and {len(r.updated) - 10} more")

    # Settings
    lines.append("")
    lines.append(f"Settings changes: {len(preview.settings)}")
    for change in preview.settings:
        marker = "⚠ " if change.dangerous else "  "
        old_str = _format_value(change.old)
        new_str = _format_value(change.new)
        lines.append(f"{marker}{change.path}: {old_str} → {new_str}")

    return "\n".join(lines)


def _bullet_list(items: list[str], prefix: str) -> list[str]:
    out = [f"{prefix}{x}" for x in items[:10]]
    if len(items) > 10:
        out.append(f"  … and {len(items) - 10} more")
    return out


def _format_value(v: object) -> str:
    if v is None:
        return "(unset)"
    if isinstance(v, str):
        # Truncate long values so the preview doesn't fill the screen
        # with a pasted shell command — the user reads the full string
        # by hovering / after import, here we just need to surface
        # "this WOULD change" and flag it for dangerous paths.
        if len(v) > 80:
            return repr(v[:77] + "…")
        return repr(v)
    return repr(v)
