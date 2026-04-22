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

import asyncio as _asyncio

from . import config, sidecar, sync as sync_mod, theme as theme_mod, voxtype_cli
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


class RestartingPill(Static):
    """Persistent in-progress indicator while a daemon restart is mid-flight.

    Distinct from StalePill: stale = "you should restart", restarting =
    "we're already doing it, hold tight". Different colors so the user can
    tell at a glance.

    The two pills are mutually exclusive — refresh_stale_pill hides the
    stale pill while restart_in_progress is True so we don't show
    contradictory signals."""

    DEFAULT_CSS = """
    RestartingPill {
        width: auto;
        height: 1;
        padding: 0 1;
        background: $accent;
        color: $background;
        text-style: bold;
        display: none;
    }
    RestartingPill.visible { display: block; }
    """


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
        yield RestartingPill(
            "⟳ Restarting daemon — loading model…",
            id="daemon-restarting",
        )
        yield Static("ctrl+s save · ctrl+r reload", id="hint")


class SyncConflictBanner(Horizontal):
    """Persistent banner shown when Syncthing has deposited
    `.sync-conflict-*.json` files next to `sync.json`. Red background,
    not dismissible — resolving requires the user to manually export
    one side and re-import the preferred state. No auto-merge (v1.1
    non-goal).

    Clicking the banner fires a toast listing the conflict file paths
    so the user can find them."""

    DEFAULT_CSS = """
    SyncConflictBanner {
        height: auto;
        background: $error 30%;
        color: $text;
        padding: 0 1;
        border-bottom: solid $error;
    }
    SyncConflictBanner > #msg { width: 1fr; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="msg")

    def on_mount(self) -> None:
        self.display = False
        self.conflict_files: list[Path] = []

    def set_conflicts(self, paths: list[Path]) -> None:
        self.conflict_files = list(paths)
        if not paths:
            self.display = False
            return
        self.query_one("#msg", Static).update(
            f"⚠ Sync conflict detected ({len(paths)} file"
            f"{'s' if len(paths) != 1 else ''}). Resolve manually — "
            "click for details."
        )
        self.display = True

    def on_click(self) -> None:
        if not self.conflict_files:
            return
        lines = "\n".join(str(p) for p in self.conflict_files)
        self.app.notify(
            lines,
            title="Sync conflict files",
            severity="warning",
            timeout=15,
        )


class AppliedSyncBanner(Horizontal):
    """One-shot banner after a successful sync-apply at startup.
    Dismissible; fades from the UI when the user clicks Dismiss."""

    DEFAULT_CSS = """
    AppliedSyncBanner {
        height: auto;
        background: $success 20%;
        color: $text;
        padding: 0 1;
        border-bottom: solid $success;
    }
    AppliedSyncBanner > #msg { width: 1fr; padding: 0 1; }
    AppliedSyncBanner > Button { height: 1; min-width: 9; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="msg")
        yield Button("Dismiss", id="dismiss-synced", variant="default")

    def on_mount(self) -> None:
        self.display = False

    def set_applied(
        self,
        device_label: str,
        settings_changes: list[str] | None = None,
        suppressed_changes: list[str] | None = None,
        applied_anything: bool = True,
    ) -> None:
        """Render the banner. Three cases drive different headlines:

          * applied_anything=True, no suppressed → "Synced from X — Ctrl+S to accept"
          * applied_anything=False, suppressed only → "Sync would have overwritten N — kept local"
          * both → "Synced from X (kept N local — Ctrl+S to accept the rest)"

        The previous "Ctrl+S to accept" was misleading in the
        suppressed-only case because there was nothing to accept; the
        drift fix already rewrites sync.json automatically so the
        banner is purely informational.
        """
        if applied_anything and suppressed_changes:
            headline = (
                f"✓ Synced from {device_label} "
                f"(kept {len(suppressed_changes)} local) — press Ctrl+S to accept the rest"
            )
        elif applied_anything:
            headline = f"✓ Synced from {device_label} — press Ctrl+S to accept"
        elif suppressed_changes:
            headline = (
                f"ℹ Sync would have overwritten {len(suppressed_changes)} setting"
                f"{'s' if len(suppressed_changes) != 1 else ''} — kept your local values"
            )
        else:
            headline = "✓ Synced"
        lines = [headline]
        if settings_changes:
            lines.append(f"  Settings changed ({len(settings_changes)}):")
            for change in settings_changes:
                lines.append(f"    • {change}")
        if suppressed_changes:
            for change in suppressed_changes:
                lines.append(f"    • {change}")
        self.query_one("#msg", Static).update("\n".join(lines))
        self.display = True

    def hide_banner(self) -> None:
        self.display = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dismiss-synced":
            self.hide_banner()
            event.stop()


class MissingModelBanner(Horizontal):
    """Three-stage banner for a sync-applied model that isn't on disk:

      1. **missing**     — initial. "Model X not downloaded" + Download/Dismiss.
      2. **downloading** — Download button disabled, label swapped for
                           "Downloading…". Progress lives in the Models tab.
      3. **downloaded**  — "✓ Downloaded X" + Set active / Dismiss.

    [Set active] flips `whisper.model` in the shadow state and dirty's
    the config. User's subsequent Ctrl+S saves + the existing
    RestartModal fires (whisper.model ∈ RESTART_SENSITIVE_PATHS).
    """

    DEFAULT_CSS = """
    MissingModelBanner {
        height: auto;
        background: $warning 25%;
        color: $text;
        padding: 0 1;
        border-bottom: solid $warning;
    }
    MissingModelBanner > #msg { width: 1fr; padding: 0 1; }
    MissingModelBanner > Button { height: 1; min-width: 10; margin-left: 1; }
    MissingModelBanner Button.hidden { display: none; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="msg")
        yield Button("Download", variant="primary", id="missing-download")
        yield Button("Set active", variant="primary", id="missing-set-active")
        yield Button("Dismiss", id="missing-dismiss", variant="default")

    def on_mount(self) -> None:
        self.display = False
        self.model_name: str | None = None
        self.query_one("#missing-set-active", Button).add_class("hidden")

    def set_missing(self, model_name: str) -> None:
        self.model_name = model_name
        self.query_one("#msg", Static).update(
            f"Model '{model_name}' is not downloaded locally. "
            "Transcription will fail until it's fetched."
        )
        dl = self.query_one("#missing-download", Button)
        dl.label = "Download"
        dl.disabled = False
        dl.remove_class("hidden")
        self.query_one("#missing-set-active", Button).add_class("hidden")
        self.display = True

    def set_downloading(self, model_name: str) -> None:
        self.model_name = model_name
        self.query_one("#msg", Static).update(
            f"Downloading {model_name}… watch the Models tab for progress."
        )
        dl = self.query_one("#missing-download", Button)
        dl.label = "Downloading…"
        dl.disabled = True
        dl.remove_class("hidden")
        self.query_one("#missing-set-active", Button).add_class("hidden")
        self.display = True

    def set_downloaded(self, model_name: str) -> None:
        self.model_name = model_name
        self.query_one("#msg", Static).update(
            f"✓ Downloaded {model_name}. Click Set active to use it."
        )
        self.query_one("#missing-download", Button).add_class("hidden")
        sa = self.query_one("#missing-set-active", Button)
        sa.disabled = False
        sa.remove_class("hidden")
        self.display = True

    def hide_banner(self) -> None:
        self.display = False
        self.model_name = None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Dismiss is the only action that fully belongs to the banner.
        # Download + Set active require app-level state (Models tab,
        # AppState mutation) so we let those events bubble to the app
        # and handle them there.
        if event.button.id == "missing-dismiss":
            self.hide_banner()
            event.stop()


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
    # True from the moment _do_restart begins until the daemon's state
    # file reports a ready state (or the readiness wait times out). Used
    # to drive the RestartingPill, the "starting" status label, and the
    # quit / re-entry guards.
    restart_in_progress: reactive[bool] = reactive(False)

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
        yield SyncConflictBanner(id="conflict-banner")
        yield AppliedSyncBanner(id="applied-banner")
        yield MissingModelBanner(id="missing-model-banner")
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

        # Drive the sync banners from the reconcile result. Conflict
        # never auto-clears (user resolves explicitly); applied +
        # missing-model are dismissible. Every reload goes through this
        # path so Ctrl+R after resolving conflicts does the right thing.
        self._update_sync_banners()
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

    def _update_sync_banners(self) -> None:
        """Reflect state.sync_reconcile in the three sync banners.
        Safe to call from any reload path — a no-op reconcile clears
        all three banners (conflict goes back to hidden; applied +
        missing close if previously open).
        """
        if self.state is None:
            return
        result = self.state.sync_reconcile

        try:
            conflict = self.query_one(SyncConflictBanner)
            applied = self.query_one(AppliedSyncBanner)
            missing = self.query_one(MissingModelBanner)
        except Exception:
            # Banners not yet mounted (very early in on_mount). Skip;
            # load_state will re-run post-mount when they exist.
            return

        conflict.set_conflicts(result.conflict_files)
        if result.applied_from or result.suppressed_settings_changes:
            applied.set_applied(
                result.applied_from or "(local)",
                settings_changes=result.applied_settings_changes,
                suppressed_changes=result.suppressed_settings_changes,
                applied_anything=result.applied_from is not None,
            )
        else:
            applied.hide_banner()
        if result.missing_model:
            missing.set_missing(result.missing_model)
        else:
            missing.hide_banner()

    def refresh_stale_pill(self) -> None:
        """Toggle the header's daemon-stale pill based on state.daemon_stale.
        Safe to call before the header has mounted (e.g. during early
        on_mount sequencing) — missing widget is a no-op.

        Hidden while restart_in_progress so the user doesn't see both
        "restart needed" and "restarting…" at the same time."""
        try:
            pill = self.query_one("#daemon-stale", StalePill)
        except Exception:
            return
        show = bool(self.state and self.state.daemon_stale) and not self.restart_in_progress
        if show:
            pill.add_class("visible")
        else:
            pill.remove_class("visible")

    def refresh_restart_pill(self) -> None:
        """Toggle the header's in-progress pill based on restart_in_progress.
        Safe to call before the header has mounted — missing widget is a
        no-op."""
        try:
            pill = self.query_one("#daemon-restarting", RestartingPill)
        except Exception:
            return
        if self.restart_in_progress:
            pill.add_class("visible")
        else:
            pill.remove_class("visible")

    def poll_daemon_state(self) -> None:
        self.daemon_state = voxtype_cli.read_state() or "no-daemon"

    def watch_daemon_state(self, state: str) -> None:
        self._render_status()

    def watch_restart_in_progress(self, in_progress: bool) -> None:
        """When the in-flight flag flips, refresh the three things it
        drives: the in-progress pill, the stale pill (which hides while
        in-flight), and the status label (which says 'starting' instead
        of 'no-daemon' until the daemon writes its state file)."""
        self.refresh_restart_pill()
        self.refresh_stale_pill()
        self._render_status()

    def _render_status(self) -> None:
        """Render the header status label. While a restart is in flight
        and the daemon hasn't written its state file yet, show 'starting'
        instead of 'no-daemon' so the user understands the daemon is
        coming back up rather than thinking it died."""
        try:
            widget = self.query_one("#status", Static)
        except Exception:
            return
        state = self.daemon_state
        if self.restart_in_progress and state in ("?", "no-daemon"):
            widget.update("⟳ starting")
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
        if self.restart_in_progress:
            # Clearer than the old "already up to date" message which
            # would fire here because _do_restart cleared daemon_stale
            # before the readiness wait completed.
            self.notify("Daemon restart already in progress…", timeout=2)
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
        """Drive the full restart cycle, from systemctl through model load.

        Two-phase: (1) systemctl restart returns ~instantly with the unit
        marked active, (2) wait for the daemon to write a ready state
        (idle/recording/transcribing) to its state file. The pill +
        "starting" status label cover the gap so the user sees what's
        happening instead of staring at a frozen-looking "no-daemon".

        Re-entry guarded — clicking the pill again mid-restart is a no-op
        with a toast, not a parallel restart."""
        if self.restart_in_progress:
            return
        self.restart_in_progress = True
        try:
            self.notify("Restarting voxtype daemon…", timeout=2)
            ok, msg = await voxtype_cli.restart_daemon_async()
            if not ok:
                self.notify(
                    msg or "restart failed",
                    severity="error",
                    timeout=10,
                )
                return
            # systemctl reports active; the daemon is alive but not
            # necessarily serving yet. Clear the stale flag now (we DID
            # restart) but keep the in-progress pill up until the state
            # file flips to a ready state. Explicit refresh — the
            # watcher on restart_in_progress also calls this in finally,
            # but doing it here too means the stale pill disappears the
            # moment systemctl confirms the restart kicked off, not
            # 5–15s later when the model finishes loading.
            if self.state is not None:
                self.state.daemon_stale = False
                self.refresh_stale_pill()
            ready = await voxtype_cli.wait_for_daemon_ready_async()
            # Refresh the polled state immediately so the header status
            # catches up without waiting for the next 0.5s tick.
            self.poll_daemon_state()
            if ready:
                self.notify("Voxtype daemon ready", timeout=3)
            else:
                self.notify(
                    "Daemon restarted but didn't report ready within 20s — "
                    "check `systemctl --user status voxtype`",
                    severity="warning",
                    timeout=10,
                )
        finally:
            # Always release the in-flight flag, even if the readiness
            # poll raised. Otherwise a stuck flag would block all future
            # restarts and quits.
            self.restart_in_progress = False

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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """App-level button handling for the missing-model banner.
        The banner itself only consumes Dismiss; Download + Set active
        bubble here because they need app state (Models tab, AppState
        mutation) the banner shouldn't reach into directly."""
        bid = event.button.id
        if bid == "missing-download":
            event.stop()
            banner = self.query_one(MissingModelBanner)
            if banner.model_name:
                _asyncio.create_task(
                    self._begin_missing_model_download(banner.model_name)
                )
        elif bid == "missing-set-active":
            event.stop()
            banner = self.query_one(MissingModelBanner)
            if banner.model_name:
                self._apply_missing_model_set_active(banner.model_name)

    async def _begin_missing_model_download(self, name: str) -> None:
        """Hand off to the Models tab's existing download pipeline so
        progress lives in one place. Flipping the engine Select ensures
        the pipeline's `engine != "whisper"` guard passes — every
        missing model reported by the sync reader is a whisper model
        (the reader only checks `whisper.model`)."""
        banner = self.query_one(MissingModelBanner)
        banner.set_downloading(name)

        # Switch to Models tab so the RichLog + ProgressBar are visible
        # to the user during the download.
        self.action_switch_tab("models")
        # Let the tab-switch and repaint settle before we kick off the
        # subprocess — otherwise the first progress frames land in a
        # still-hidden pane and feel janky.
        await _asyncio.sleep(0)

        try:
            pane = self.query_one(ModelsPane)
        except Exception:
            banner.set_missing(name)
            return

        from textual.widgets import Select
        engine_select = pane.query_one("#models-engine", Select)
        if engine_select.value != "whisper":
            engine_select.value = "whisper"
            pane.refresh_table()
            await _asyncio.sleep(0)

        try:
            await pane._run_download(name)
        except _asyncio.CancelledError:
            banner.set_missing(name)
            raise
        except Exception as e:
            banner.set_missing(name)
            self.notify(f"Download failed: {e}", severity="error", timeout=10)
            return

        if sync_mod._model_file_present(name, sync_mod.DEFAULT_MODELS_DIR):
            banner.set_downloaded(name)
        else:
            # Download subprocess exited without producing the file
            # (exit code != 0, cancelled, etc.). _run_download already
            # surfaced a toast; reset the banner so the user can retry.
            banner.set_missing(name)

    def _apply_missing_model_set_active(self, name: str | None) -> None:
        """Promote the downloaded model to active in the shadow state.
        The user still has to Ctrl+S to persist; RestartModal then
        fires automatically because `whisper.model` is restart-sensitive.

        Guarded: if the download subprocess finished but the file isn't
        actually on disk (interrupted download, wrong URL, curl exited
        non-zero, etc.), we refuse to set it active — that would leave
        the daemon pointing at a missing file and fail at next restart.
        """
        if self.state is None or not name:
            return
        from .models import is_model_installed
        if not is_model_installed("whisper", name):
            self.notify(
                f"'{name}' didn't finish downloading — refusing to "
                f"activate. Retry the download from the Models tab.",
                title="Download incomplete",
                severity="warning",
                timeout=8,
            )
            return
        self.state.set_setting("whisper.model", name)
        self.refresh_dirty()
        # Hide the banner — the workflow is now complete; the user's
        # next step (Ctrl+S) is independent and signposted in the toast.
        self.query_one(MissingModelBanner).hide_banner()
        self.notify(
            f"'{name}' is now the active whisper model. "
            "Press Ctrl+S to save — the restart modal will follow.",
            title="Set active",
            timeout=8,
        )

    def action_request_quit(self) -> None:
        if self.restart_in_progress:
            # self.exit() during an in-flight to_thread doesn't take effect
            # until the task drains, so the old behavior was that Q sat
            # silently for 5–15s and then the app vanished. Refusing
            # explicitly is much less confusing.
            self.notify(
                "Restart in progress — wait for the daemon to be ready, "
                "then press Q again",
                severity="warning",
                timeout=4,
            )
            return
        if self.state is None or not self.state.dirty:
            self.exit()
            return
        def after(confirmed: bool | None) -> None:
            if confirmed:
                self.exit()
        self.push_screen(ConfirmQuitModal(), after)


def main() -> None:
    import sys
    if "--apply-migrations" in sys.argv[1:]:
        from .cli_migrate import main as migrate_main
        sys.exit(migrate_main(sys.argv[1:]))
    # Refuse to launch when another voxtype-tui is already running.
    # Two concurrent TUIs against the same `~/.config/voxtype/config.toml`
    # silently lose updates (no fcntl lock around save). Most realistic
    # repro: the AUR install + a conda dev env open simultaneously.
    from . import single_instance
    lock = single_instance.acquire()
    if not lock.acquired:
        holder = (
            f"PID {lock.holder_pid}" if lock.holder_pid else "another instance"
        )
        print(
            f"voxtype-tui: another instance is already running ({holder}). "
            f"Refusing to launch — concurrent TUIs lose updates silently.",
            file=sys.stderr,
        )
        sys.exit(1)
    app = VoxtypeTUI()
    app.run()
    _restart_daemon_on_exit_if_needed(app)


def _restart_daemon_on_exit_if_needed(app: "VoxtypeTUI") -> None:
    """Auto-restart the voxtype daemon after the TUI exits, if and only if
    the user saved any config.toml change during this session.

    Rationale: Voxtype caches its config into memory at daemon start
    (``src/text/mod.rs:27-40`` + ``src/daemon.rs`` Daemon::new), so any
    config write sits dormant until the daemon reloads. The in-TUI pill
    + Ctrl+Shift+R path handles the immediate-feedback case; this hook
    handles the "user tweaked five settings and closed the TUI without
    clicking the pill" case. Sidecar-only edits (category flips, notes)
    don't trigger — the post_process CLI re-reads metadata.json on every
    transcription, no restart needed.

    Spawns the restart synchronously but with a short timeout so the
    shell prompt returns fast (~500ms typical). A one-line notice tells
    the user what happened — silent auto-actions without a paper trail
    are unfriendly. Failures print but don't propagate (user already
    closed the UI; we don't want to crash on the way out).
    """
    state = getattr(app, "state", None)
    if state is None or not state.daemon_stale:
        return
    if not voxtype_cli.is_daemon_active():
        return
    print("voxtype-tui: restarting voxtype daemon…")
    ok, msg = voxtype_cli.restart_daemon()
    if ok:
        print("voxtype-tui: voxtype daemon restarted.")
    else:
        print(f"voxtype-tui: daemon restart failed: {msg}")
        print("voxtype-tui: run 'systemctl --user restart voxtype' manually.")


if __name__ == "__main__":
    main()
