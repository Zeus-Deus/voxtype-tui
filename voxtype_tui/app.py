"""Voxtype-TUI Textual shell.

This is the cross-cutting shell only: three empty tabs, a status-polling
header, dirty indicator, reconcile-warning banner, and the save flow
(safe_save → restart-diff → restart modal). Tab content lives in
screens/vocabulary.py, screens/dictionary.py, screens/settings.py and is
swapped in later.
"""
from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, Static, TabbedContent, TabPane

from . import config, sidecar, voxtype_cli
from .dictionary import DictionaryPane
from .models import ModelsPane
from .settings import SettingsPane
from .state import AppState
from .vocabulary import VocabularyPane

STATUS_ICONS = {
    "idle": "●",
    "recording": "⏺",
    "transcribing": "⋯",
    "no-daemon": "○",
}


class HeaderBar(Horizontal):
    DEFAULT_CSS = """
    HeaderBar {
        height: 1;
        background: $primary;
        color: $text;
    }
    HeaderBar > Static {
        height: 1;
        padding: 0 1;
    }
    HeaderBar > #title { width: auto; text-style: bold; }
    HeaderBar > #status { width: auto; }
    HeaderBar > #dirty { width: auto; }
    HeaderBar > #hint { width: 1fr; text-align: right; color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        yield Static("voxtype-tui", id="title")
        yield Static("○ ?", id="status")
        yield Static("✓ saved", id="dirty")
        yield Static("ctrl+s save · ctrl+r reload", id="hint")


class ReconcileBanner(Horizontal):
    DEFAULT_CSS = """
    ReconcileBanner {
        height: auto;
        background: $warning 20%;
        color: $text;
        padding: 0 1;
        border-bottom: solid $warning;
    }
    ReconcileBanner > #msg { width: 1fr; padding: 0 1; }
    ReconcileBanner > Button { height: 1; min-width: 9; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="msg")
        yield Button("Dismiss", id="dismiss", variant="default")

    def on_mount(self) -> None:
        # Hide via the `display` property so descendants (Dismiss button) are
        # excluded from the focus chain when the banner isn't active.
        self.display = False

    def show_warnings(self, messages: list[str]) -> None:
        self.query_one("#msg", Static).update(
            "⚠  " + "  ·  ".join(messages)
        )
        self.display = True

    def hide_banner(self) -> None:
        self.display = False

    def has_class(self, name: str) -> bool:
        # Back-compat for tests checking visibility via has_class("visible").
        if name == "visible":
            return bool(self.display)
        return super().has_class(name)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dismiss":
            self.hide_banner()
            event.stop()


class RestartModal(ModalScreen[bool]):
    """Post-save prompt when restart-sensitive fields changed."""

    DEFAULT_CSS = """
    RestartModal { align: center middle; }
    RestartModal > Vertical {
        background: $panel;
        border: thick $accent;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    RestartModal #title { text-style: bold; margin-bottom: 1; }
    RestartModal #fields { color: $text-muted; margin-bottom: 1; }
    RestartModal Horizontal { height: auto; align: center middle; }
    RestartModal Button { margin: 0 1; }
    """

    def __init__(self, changed_fields: list[str]) -> None:
        super().__init__()
        self.changed_fields = changed_fields

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Restart Voxtype daemon?", id="title")
            yield Label(
                "These changes require a restart to take effect:\n\n  • "
                + "\n  • ".join(self.changed_fields),
                id="fields",
            )
            with Horizontal():
                yield Button("Restart now", variant="primary", id="yes")
                yield Button("Later", variant="default", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class ConfirmQuitModal(ModalScreen[bool]):
    """Shown when the user tries to quit with unsaved changes."""

    DEFAULT_CSS = """
    ConfirmQuitModal { align: center middle; }
    ConfirmQuitModal > Vertical {
        background: $panel;
        border: thick $warning;
        padding: 1 2;
        width: 50;
        height: auto;
    }
    ConfirmQuitModal #title { text-style: bold; margin-bottom: 1; }
    ConfirmQuitModal Horizontal { height: auto; align: center middle; }
    ConfirmQuitModal Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("y", "confirm", "Quit", show=False),
        Binding("n,escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Unsaved changes — quit anyway?", id="title")
            with Horizontal():
                yield Button("Quit (y)", variant="warning", id="yes")
                yield Button("Cancel (n)", variant="default", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class VoxtypeTUI(App[None]):
    TITLE = "voxtype-tui"

    CSS = """
    Screen { background: $surface; }
    TabbedContent { height: 1fr; }
    """

    BINDINGS = [
        Binding("1", "switch_tab('vocabulary')", "Vocab", priority=True),
        Binding("2", "switch_tab('dictionary')", "Dict", priority=True),
        Binding("3", "switch_tab('settings')", "Settings", priority=True),
        Binding("4", "switch_tab('models')", "Models", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+r", "reload_config", "Reload", priority=True),
        Binding("ctrl+t", "test_mutate", "Fake mutation", show=False),
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
    ]

    daemon_state: reactive[str] = reactive("?")

    def __init__(
        self,
        config_path: Path | None = None,
        sidecar_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.config_path = config_path or config.CONFIG_PATH
        self.sidecar_path = sidecar_path or sidecar.SIDECAR_PATH
        self.state: AppState | None = None

    def compose(self) -> ComposeResult:
        yield HeaderBar()
        yield ReconcileBanner(id="banner")
        with TabbedContent(id="tabs", initial="vocabulary"):
            with TabPane("Vocabulary", id="vocabulary"):
                yield VocabularyPane()
            with TabPane("Dictionary", id="dictionary"):
                yield DictionaryPane()
            with TabPane("Settings", id="settings"):
                yield SettingsPane()
            with TabPane("Models", id="models"):
                yield ModelsPane()
        yield Footer()

    def on_mount(self) -> None:
        self.load_state()
        self.set_interval(0.5, self.poll_daemon_state)
        self.poll_daemon_state()
        # Default focus to the first tab's main widget so the letter
        # bindings (n / / / d / u) work without a manual click.
        try:
            pane = self.query_one(VocabularyPane)
            from textual.widgets import DataTable
            self.set_focus(pane.query_one(DataTable))
        except Exception:
            pass

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Don't steal digit keys while an Input widget has focus — otherwise
        # typing a phrase with a number in it jumps tabs.
        if action == "switch_tab" and isinstance(self.focused, Input):
            return False
        return True

    def load_state(self) -> None:
        try:
            self.state = AppState.load(self.config_path, self.sidecar_path)
        except Exception as e:
            self.notify(f"Load failed: {e}", severity="error", timeout=10)
            return
        banner = self.query_one(ReconcileBanner)
        if self.state.reconcile_warnings:
            banner.show_warnings(self.state.reconcile_warnings)
        else:
            banner.hide_banner()
        self.refresh_dirty()
        # Give each tab a chance to resync to the freshly-loaded state.
        for pane in self.query(VocabularyPane):
            pane.sync_from_state()
        for pane in self.query(DictionaryPane):
            pane.sync_from_state()
        for pane in self.query(SettingsPane):
            pane.sync_from_state()
        for pane in self.query(ModelsPane):
            pane.sync_from_state()

    def refresh_dirty(self) -> None:
        widget = self.query_one("#dirty", Static)
        if self.state and self.state.dirty:
            widget.update("● dirty")
        else:
            widget.update("✓ saved")

    def poll_daemon_state(self) -> None:
        self.daemon_state = voxtype_cli.read_state() or "no-daemon"

    def watch_daemon_state(self, state: str) -> None:
        try:
            widget = self.query_one("#status", Static)
        except Exception:
            return
        icon = STATUS_ICONS.get(state, "?")
        widget.update(f"{icon} {state}")

    # --- actions ---

    def action_switch_tab(self, tab_id: str) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        # Textual reverts `active` if the focused widget still lives in the
        # previous pane. Clear focus first, then point it at the target pane's
        # main navigation surface. Prefer a DataTable — landing on an Input
        # would make digit keys insert text instead of switching tabs.
        self.set_focus(None)
        tabs.active = tab_id
        try:
            from textual.widgets import DataTable
            pane = tabs.get_pane(tab_id)
            tables = list(pane.query(DataTable))
            if tables:
                self.set_focus(tables[0])
                return
            for w in pane.query("*"):
                if getattr(w, "can_focus", False) and not isinstance(w, Input):
                    self.set_focus(w)
                    return
        except Exception:
            pass

    async def action_save(self) -> None:
        if self.state is None:
            return
        if not self.state.dirty:
            self.notify("Nothing to save")
            return
        try:
            restart_fields = await self.state.save_async()
        except config.ValidationError as e:
            self.notify(
                f"Save rejected by voxtype:\n{e}",
                title="Invalid config",
                severity="error",
                timeout=12,
            )
            return
        except Exception as e:
            self.notify(f"Save failed: {e}", severity="error", timeout=10)
            return
        self.refresh_dirty()
        daemon_active = await voxtype_cli.is_daemon_active_async()
        if restart_fields and daemon_active:
            async def after(do_restart: bool | None) -> None:
                if not do_restart:
                    return
                self.notify("Restarting voxtype…", timeout=3)
                ok, msg = await voxtype_cli.restart_daemon_async()
                self.notify(
                    msg,
                    severity="information" if ok else "error",
                    timeout=5 if ok else 10,
                )
            self.push_screen(RestartModal(restart_fields), after)
        else:
            self.notify("Saved", timeout=2)

    def action_reload_config(self) -> None:
        if self.state and self.state.dirty:
            self.notify(
                "Unsaved changes — save (ctrl+s) or quit to discard",
                severity="warning",
                timeout=5,
            )
            return
        self.load_state()
        self.notify("Reloaded")

    def action_test_mutate(self) -> None:
        """Dev-only: forces a dirty state so we can smoke-test the save flow
        before tab content exists."""
        if self.state is None:
            return
        count = len(self.state.sc.vocabulary)
        self.state.add_vocab(f"TestWord{count}")
        self.refresh_dirty()
        self.notify(f"Added TestWord{count} (not saved)")

    def action_request_quit(self) -> None:
        if self.state is None or not self.state.dirty:
            self.exit()
            return
        def after(confirmed: bool | None) -> None:
            if confirmed:
                self.exit()
        self.push_screen(ConfirmQuitModal(), after)


def main() -> None:
    VoxtypeTUI().run()


if __name__ == "__main__":
    main()
