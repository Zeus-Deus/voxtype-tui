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

from . import config, sidecar, theme as theme_mod, voxtype_cli
from .theme import MODAL_BORDER_STYLE
from .dictionary import DictionaryPane
from .models import ModelsPane
from .screens.export import ExportBundleModal
from .screens.import_bundle import ImportBundleModal
from .settings import SettingsPane
from .state import AppState
from .vocabulary import VocabularyPane

STATUS_ICONS = {
    "idle": "●",
    "recording": "⏺",
    "transcribing": "⋯",
    "no-daemon": "○",
}


class StalePill(Static):
    """Persistent warning in the header bar whenever the voxtype daemon is
    running with stale config (a restart-sensitive field was saved and the
    daemon hasn't been restarted since). Click fires the same action as
    ctrl+shift+r."""

    DEFAULT_CSS = """
    StalePill {
        width: auto;
        height: 1;
        padding: 0 1;
        background: $warning;
        color: $background;
        text-style: bold;
        display: none;
    }
    StalePill.visible { display: block; }
    """

    def on_click(self) -> None:
        self.app.action_restart_daemon()


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
        yield StalePill(
            "⚠ Daemon restart needed (Ctrl+Shift+R)",
            id="daemon-stale",
        )
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

    DEFAULT_CSS = f"""
    RestartModal {{ align: center middle; }}
    RestartModal > Vertical {{
        background: $panel;
        border: {MODAL_BORDER_STYLE} $accent;
        padding: 1 2;
        width: 60;
        height: auto;
    }}
    RestartModal #title {{ text-style: bold; margin-bottom: 1; }}
    RestartModal #fields {{ color: $text-muted; margin-bottom: 1; }}
    RestartModal Horizontal {{ height: auto; align: center middle; }}
    RestartModal Button {{ margin: 0 1; }}
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

    DEFAULT_CSS = f"""
    ConfirmQuitModal {{ align: center middle; }}
    ConfirmQuitModal > Vertical {{
        background: $panel;
        border: {MODAL_BORDER_STYLE} $warning;
        padding: 1 2;
        width: 50;
        height: auto;
    }}
    ConfirmQuitModal #title {{ text-style: bold; margin-bottom: 1; }}
    ConfirmQuitModal Horizontal {{ height: auto; align: center middle; }}
    ConfirmQuitModal Button {{ margin: 0 1; }}
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
        Binding("ctrl+shift+r", "restart_daemon", "Restart daemon", priority=True),
        Binding("ctrl+e", "export_bundle", "Export", priority=True),
        Binding("ctrl+i", "import_bundle", "Import", priority=True),
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
        self._register_themes()
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
        # Ctrl+E shouldn't fire from inside an Input either. Textual's
        # priority bindings still consume the keystroke unless check_action
        # returns False — e.g. typing an 'E' char elsewhere is fine, but
        # Ctrl+E while editing a vocab phrase is almost always a typo and
        # opening the export modal would clobber the user's input focus.
        if action == "export_bundle" and isinstance(self.focused, Input):
            return False
        if action == "import_bundle" and isinstance(self.focused, Input):
            return False
        return True

    def _register_themes(self) -> None:
        """Register Omarchy / user themes if available, and set the initial
        theme from ui.json."""
        default_name = "textual-dark"

        om_colors = theme_mod.load_omarchy_colors()
        if om_colors:
            self.register_theme(theme_mod.build_theme("omarchy-auto", om_colors))
            default_name = "omarchy-auto"

        user_colors = theme_mod.load_user_colors()
        if user_colors:
            self.register_theme(theme_mod.build_theme("user", user_colors))
            default_name = "user"

        prefs = theme_mod.load_ui_prefs()
        saved = prefs.get("theme", default_name)
        try:
            self.theme = saved
        except Exception:
            self.theme = default_name

    def watch_theme(self, new_theme: str) -> None:
        """Textual fires this on theme change — persist so the choice
        survives restart."""
        prefs = theme_mod.load_ui_prefs()
        if prefs.get("theme") == new_theme:
            return
        prefs["theme"] = new_theme
        try:
            theme_mod.save_ui_prefs(prefs)
        except OSError:
            pass

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

    def refresh_stale_pill(self) -> None:
        """Toggle the header's daemon-stale pill based on state.daemon_stale.
        Safe to call before the header has mounted (e.g. during early
        on_mount sequencing) — missing widget is a no-op."""
        try:
            pill = self.query_one("#daemon-stale", StalePill)
        except Exception:
            return
        if self.state and self.state.daemon_stale:
            pill.add_class("visible")
        else:
            pill.remove_class("visible")

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
        was_stale = self.state.daemon_stale
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
        self.refresh_stale_pill()

        # Modal only on the first-time transition into stale state. Once the
        # pill is up the user already has a persistent signal — further
        # saves during the stale session just toast.
        newly_stale = bool(restart_fields) and not was_stale
        if not restart_fields:
            self.notify("Saved", timeout=2)
            return
        daemon_active = await voxtype_cli.is_daemon_active_async()
        if not daemon_active:
            self.notify("Saved — daemon isn't running", timeout=3)
            return
        if newly_stale:
            async def after(do_restart: bool | None) -> None:
                if not do_restart:
                    return
                await self._do_restart()
            self.push_screen(RestartModal(restart_fields), after)
        else:
            self.notify(
                "Saved — daemon still needs restart (ctrl+shift+r)",
                timeout=4,
            )

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

    async def action_restart_daemon(self) -> None:
        """Manually triggered by ctrl+shift+r or a click on the stale pill.
        No-op when the daemon isn't stale (no damage if the user just hammers
        the binding)."""
        if self.state is None:
            return
        if not self.state.daemon_stale:
            self.notify("Daemon is already up to date", timeout=2)
            return
        if not await voxtype_cli.is_daemon_active_async():
            self.notify(
                "Daemon isn't running — nothing to restart",
                severity="warning", timeout=4,
            )
            # Clear the flag so the pill doesn't mislead the user.
            self.state.daemon_stale = False
            self.refresh_stale_pill()
            return
        await self._do_restart()

    async def _do_restart(self) -> None:
        self.notify("Restarting voxtype daemon…", timeout=3)
        ok, msg = await voxtype_cli.restart_daemon_async()
        if ok and self.state is not None:
            self.state.daemon_stale = False
            self.refresh_stale_pill()
        self.notify(
            msg,
            severity="information" if ok else "error",
            timeout=5 if ok else 10,
        )

    def action_export_bundle(self) -> None:
        """Open the manual-export modal. Dismissal callback surfaces the
        resulting path as a toast; cancellation is silent. The modal
        itself handles write failures in-place (error label) so the user
        can fix a bad path without a reopen."""
        if self.state is None:
            return

        def after(path: Path | None) -> None:
            if path is None:
                return
            self.notify(
                f"Exported to {path}",
                title="Export complete",
                timeout=6,
            )

        self.push_screen(ExportBundleModal(self.state), after)

    def action_import_bundle(self) -> None:
        """Open the manual-import modal. On apply, the shadow state is
        merged in-place and dirty flags flip — user still needs Ctrl+S
        to persist. We re-sync every tab so the newly-merged vocab,
        replacements, and settings land in the UI immediately."""
        if self.state is None:
            return

        def after(applied: bool | None) -> None:
            if not applied:
                return
            # Refresh each pane so merged content shows up without a
            # reload. refresh_dirty flips the top-bar pill to `● dirty`
            # — the save action is the user's next step.
            for pane in self.query(VocabularyPane):
                pane.sync_from_state()
            for pane in self.query(DictionaryPane):
                pane.sync_from_state()
            for pane in self.query(SettingsPane):
                pane.sync_from_state()
            for pane in self.query(ModelsPane):
                pane.sync_from_state()
            self.refresh_dirty()
            self.notify(
                "Imported. Press Ctrl+S to persist.",
                title="Import applied",
                timeout=6,
            )

        self.push_screen(ImportBundleModal(self.state), after)

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
