"""Models tab — list / download / delete / set-active for transcription models.

Voxtype's download command (`voxtype setup --download --model <name>`) is
"download AND make active" — it writes to both ~/.local/share/voxtype/models/
and ~/.config/voxtype/config.toml. To avoid silently clobbering a user's
unsaved in-memory changes, Download is disabled whenever the app's state is
dirty; the user must ctrl+s first. After a successful download the state
is reloaded from disk so voxtype's config edit is reflected.

Set-active is the in-app alternative that only flips config_dirty (like any
other Settings edit) — user still ctrl+s's to persist, and the existing
RestartModal handles the daemon restart since whisper.model / parakeet.
model_type etc. are all in the restart-sensitive list.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
)

from . import config
from .settings import MODEL_PATH_PER_ENGINE
from .theme import MODAL_BORDER_STYLE
from .vim_nav import VIM_NAV_ACTIONS, VIM_NAV_BINDINGS, VimTableNav

if TYPE_CHECKING:
    from .app import VoxtypeTUI


MODELS_DIR = Path.home() / ".local" / "share" / "voxtype" / "models"


@dataclass(frozen=True)
class ModelInfo:
    name: str
    size_mb: int


# Per-engine model catalog. Sizes approximate (actual on-disk size shown when
# downloaded). Sourced from Whisper.cpp docs and Voxtype's setup --help.
MODEL_CATALOG: dict[str, list[ModelInfo]] = {
    "whisper": [
        ModelInfo("tiny", 75),
        ModelInfo("tiny.en", 75),
        ModelInfo("base", 140),
        ModelInfo("base.en", 140),
        ModelInfo("small", 460),
        ModelInfo("small.en", 460),
        ModelInfo("medium", 1500),
        ModelInfo("medium.en", 1500),
        ModelInfo("large-v3", 2900),
        ModelInfo("large-v3-turbo", 1500),
    ],
    "parakeet": [
        ModelInfo("parakeet-tdt-0.6b-v3", 2500),
        ModelInfo("parakeet-tdt-0.6b-v3-int8", 650),
    ],
    "moonshine": [
        ModelInfo("tiny", 150),
        ModelInfo("base", 300),
    ],
    "sensevoice": [],
    "paraformer": [],
    "dolphin": [],
    "omnilingual": [],
}

PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def parse_percent(line: str) -> float | None:
    matches = PCT_RE.findall(line)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def split_terminal_output(
    buffer: bytes,
) -> tuple[list[tuple[str, bool]], bytes]:
    """Split a chunk of subprocess output on both `\r` and `\n` so curl-style
    progress bars (which use `\r` to overwrite) are visible in real time.

    Returns (units, leftover) where each unit is (text, is_newline). A unit
    marked is_newline=False is a `\r`-terminated overwrite — it should drive
    the progress bar but NOT be logged (otherwise the log fills with 20
    redundant progress-bar frames per download). `\r\n` is folded into a
    single newline terminator. The leftover bytes should be prepended to the
    next chunk so partial units aren't lost at chunk boundaries.
    """
    units: list[tuple[str, bool]] = []
    last = 0
    i = 0
    n = len(buffer)
    while i < n:
        c = buffer[i:i + 1]
        if c == b"\n":
            segment = buffer[last:i].rstrip(b"\r")
            units.append((strip_ansi(segment.decode(errors="replace")), True))
            i += 1
            last = i
        elif c == b"\r":
            # \r\n → single newline
            if i + 1 < n and buffer[i + 1:i + 2] == b"\n":
                segment = buffer[last:i]
                units.append((strip_ansi(segment.decode(errors="replace")), True))
                i += 2
            else:
                segment = buffer[last:i]
                units.append((strip_ansi(segment.decode(errors="replace")), False))
                i += 1
            last = i
        else:
            i += 1
    return units, buffer[last:]


def model_file_path(engine: str, name: str) -> Path:
    """Predict the on-disk path Voxtype uses for a given engine + model.
    Currently only whisper is supported for on-disk operations; other engines
    fall through and return a best-guess path."""
    if engine == "whisper":
        return MODELS_DIR / f"ggml-{name}.bin"
    return MODELS_DIR / f"{name}.bin"


def scan_downloaded(engine: str) -> dict[str, int]:
    """Return {model_name: size_bytes} for models present on disk.

    For whisper, matches `ggml-<name>.bin`. Unknown `.bin` files (not in the
    catalog for any engine) are also included with name derived from the
    filename — user may want to Delete them via the tab."""
    out: dict[str, int] = {}
    if not MODELS_DIR.exists():
        return out
    for f in MODELS_DIR.glob("*.bin"):
        try:
            size = f.stat().st_size
        except OSError:
            continue
        stem = f.stem
        if engine == "whisper" and stem.startswith("ggml-"):
            out[stem.removeprefix("ggml-")] = size
        elif engine != "whisper" and not stem.startswith("ggml-"):
            out[stem] = size
    return out


def total_disk_usage() -> int:
    if not MODELS_DIR.exists():
        return 0
    total = 0
    for f in MODELS_DIR.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                continue
    return total


def humanize_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit != "B" else f"{n} {unit}"
        n //= 1024
    return f"{n} GB"


class ConfirmDeleteModal(ModalScreen[bool]):
    DEFAULT_CSS = f"""
    ConfirmDeleteModal {{ align: center middle; }}
    ConfirmDeleteModal > Vertical {{
        background: $panel;
        border: {MODAL_BORDER_STYLE} $error;
        padding: 1 2;
        width: 60;
        height: auto;
    }}
    ConfirmDeleteModal #title {{ text-style: bold; margin-bottom: 1; }}
    ConfirmDeleteModal #body {{ color: $text-muted; margin-bottom: 1; }}
    ConfirmDeleteModal Horizontal {{ height: auto; align: center middle; }}
    ConfirmDeleteModal Button {{ margin: 0 1; }}
    """

    BINDINGS = [
        Binding("y", "confirm", "Delete", show=False),
        Binding("n,escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, path: Path, size_human: str) -> None:
        super().__init__()
        self.path = path
        self.size_human = size_human

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Delete {self.path.name}?", id="title")
            yield Label(
                f"{self.size_human} will be freed. The model can be re-downloaded.",
                id="body",
            )
            with Horizontal():
                yield Button("Delete (y)", variant="error", id="yes")
                yield Button("Cancel (n)", variant="default", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ModelsPane(VimTableNav, Vertical):
    DEFAULT_CSS = """
    ModelsPane { padding: 1 2; }
    ModelsPane #models-top { height: 3; margin-bottom: 1; align: left middle; }
    ModelsPane #models-engine { width: 24; }
    ModelsPane #models-disk { width: 1fr; text-align: right; color: $text-muted; padding-right: 1; }
    ModelsPane DataTable { height: 1fr; margin-bottom: 1; }
    ModelsPane #models-buttons { height: 3; margin-bottom: 1; }
    ModelsPane #models-buttons Button { margin-right: 1; height: 3; }
    ModelsPane ProgressBar {
        height: 1;
        width: 100%;
        margin: 0 0 1 0;
    }
    ModelsPane RichLog {
        height: 10;
        background: $panel;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("d", "delete_selected", "Delete"),
        Binding("/", "focus_engine", "Engine", show=False),
    ] + VIM_NAV_BINDINGS

    def __init__(self) -> None:
        super().__init__()
        self._active_proc: asyncio.subprocess.Process | None = None
        self._active_model: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="models-top"):
            yield Label("Engine:", classes="label")
            yield Select(
                options=[(e, e) for e in MODEL_CATALOG.keys()],
                allow_blank=False,
                value="whisper",
                id="models-engine",
            )
            yield Static("", id="models-disk")
        yield DataTable(id="models-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="models-buttons"):
            yield Button("Set active", id="models-set-active")
            yield Button("Download", variant="primary", id="models-download")
            yield Button("Delete", variant="warning", id="models-delete")
            yield Button("Cancel", variant="default", id="models-cancel", disabled=True)
        yield ProgressBar(total=100.0, show_eta=False, id="models-progress")
        yield RichLog(id="models-log", markup=False, highlight=False, wrap=False)

    # ---- boilerplate ----

    @property
    def tui(self) -> "VoxtypeTUI":
        return self.app  # type: ignore[return-value]

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # `d` would collide with Settings' no-op, but here our pane is its
        # own container — only disable when the engine Select or any Input
        # descendant is focused.
        focus = self.app.focused
        from textual.widgets import Input
        if action in VIM_NAV_ACTIONS | {"delete_selected", "refresh"}:
            if isinstance(focus, Input):
                return False
        return True

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Model", "Size", "Status", "Active")
        self.refresh_table()
        self._update_disk_label()

    def sync_from_state(self) -> None:
        self.refresh_table()
        self._update_disk_label()

    # ---- selectors ----

    def _current_engine(self) -> str:
        return str(self.query_one("#models-engine", Select).value)

    def _active_model_for_engine(self) -> str:
        if self.tui.state is None:
            return ""
        engine = self._current_engine()
        path = MODEL_PATH_PER_ENGINE.get(engine)
        if path is None:
            return ""
        section, key = path.split(".")
        return str(self.tui.state.doc.get(section, {}).get(key, ""))

    def _selected_model_name(self) -> str | None:
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

    # ---- rendering ----

    def refresh_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        engine = self._current_engine()
        downloaded = scan_downloaded(engine)
        catalog = MODEL_CATALOG.get(engine, [])
        active = self._active_model_for_engine()
        seen_names: set[str] = set()
        for m in catalog:
            seen_names.add(m.name)
            status = "downloaded" if m.name in downloaded else "—"
            active_mark = "●" if m.name == active else ""
            size = _fmt_size(
                downloaded[m.name] if m.name in downloaded else m.size_mb * 1024 * 1024
            )
            table.add_row(m.name, size, status, active_mark, key=m.name)
        # Unknown .bin files on disk not in the catalog
        for name, size in downloaded.items():
            if name in seen_names:
                continue
            table.add_row(
                name, _fmt_size(size), "unknown", "",
                key=name,
            )

    def _update_disk_label(self) -> None:
        used = total_disk_usage()
        self.query_one("#models-disk", Static).update(
            f"Models dir: {humanize_bytes(used)} used"
        )

    # ---- event handlers ----

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "models-engine":
            self.refresh_table()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "models-set-active":
            event.stop()
            self._action_set_active()
        elif bid == "models-download":
            event.stop()
            self._action_download()
        elif bid == "models-delete":
            event.stop()
            self._action_delete()
        elif bid == "models-cancel":
            event.stop()
            asyncio.create_task(self._cancel_active_download())

    # ---- actions ----

    def action_refresh(self) -> None:
        self.refresh_table()
        self._update_disk_label()
        self.app.notify("Refreshed", timeout=1)

    def action_focus_engine(self) -> None:
        self.query_one("#models-engine", Select).focus()

    def action_delete_selected(self) -> None:
        self._action_delete()

    def _action_set_active(self) -> None:
        if self.tui.state is None:
            return
        name = self._selected_model_name()
        if not name:
            return
        engine = self._current_engine()
        path = MODEL_PATH_PER_ENGINE.get(engine)
        if not path:
            self.app.notify(
                f"No model-path mapping for engine '{engine}'",
                severity="warning",
            )
            return
        self.tui.state.set_setting(path, name)
        self.tui.refresh_dirty()
        self.refresh_table()
        # Settings tab's Model dropdown is hydrated only on mount / reload, so
        # nudge it now — otherwise it shows a stale value until ctrl+r.
        from .settings import SettingsPane
        for pane in self.app.query(SettingsPane):
            pane.sync_from_state()
        self.app.notify(f"'{name}' marked active (ctrl+s to save)", timeout=3)

    def _action_download(self) -> None:
        if self._active_proc is not None:
            self.app.notify("A download is already running", severity="warning")
            return
        if self.tui.state is None:
            return
        if self.tui.state.dirty:
            self.app.notify(
                "Save pending changes (ctrl+s) before downloading — "
                "voxtype writes to config.toml during download.",
                severity="warning",
                timeout=8,
                title="Unsaved changes",
            )
            return
        name = self._selected_model_name()
        if not name:
            self.app.notify("No model selected", severity="warning")
            return
        engine = self._current_engine()
        if engine != "whisper":
            # Voxtype's setup --download currently handles whisper models;
            # other engines use their own setup commands. Defer for v1.
            self.app.notify(
                f"Download-from-TUI supports whisper only in v1; use "
                f"`voxtype setup model` for {engine}.",
                severity="warning", timeout=8,
            )
            return
        asyncio.create_task(self._run_download(name))

    def _action_delete(self) -> None:
        name = self._selected_model_name()
        if not name:
            return
        engine = self._current_engine()
        if name == self._active_model_for_engine():
            self.app.notify(
                "Cannot delete the active model. Set a different model "
                "active first.",
                severity="warning", timeout=6,
            )
            return
        path = model_file_path(engine, name)
        if not path.exists():
            self.app.notify(
                f"'{name}' isn't downloaded — nothing to delete.",
                severity="warning",
            )
            return
        try:
            size = path.stat().st_size
        except OSError:
            size = 0

        def after(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                os.unlink(path)
            except OSError as e:
                self.app.notify(f"Delete failed: {e}", severity="error")
                return
            self.refresh_table()
            self._update_disk_label()
            self.app.notify(f"Deleted {path.name}", timeout=3)

        self.app.push_screen(
            ConfirmDeleteModal(path, humanize_bytes(size)),
            after,
        )

    async def _run_download(self, name: str) -> None:
        log = self.query_one("#models-log", RichLog)
        progress = self.query_one("#models-progress", ProgressBar)
        progress.update(progress=0)
        log.clear()
        log.write(f"$ voxtype setup --download --model {name}")

        try:
            proc = await asyncio.create_subprocess_exec(
                "voxtype", "setup", "--download", "--model", name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            log.write(f"ERROR: {e}")
            self.app.notify(f"Could not launch voxtype: {e}", severity="error")
            return

        self._active_proc = proc
        self._active_model = name
        self._set_buttons_downloading(True)

        try:
            assert proc.stdout is not None
            buffer = b""
            while True:
                chunk = await proc.stdout.read(256)
                if not chunk:
                    break
                buffer += chunk
                units, buffer = split_terminal_output(buffer)
                for text, is_newline in units:
                    pct = parse_percent(text)
                    if pct is not None:
                        progress.update(progress=pct)
                    if is_newline and text.strip():
                        log.write(text)
                # Explicit yield so Textual can render the updated bar/log
                # between chunks even on a tight, continuous stream.
                await asyncio.sleep(0)
            # Drain any trailing bytes that weren't terminator-delimited.
            if buffer:
                text = strip_ansi(buffer.decode(errors="replace"))
                pct = parse_percent(text)
                if pct is not None:
                    progress.update(progress=pct)
                if text.strip():
                    log.write(text)
        except asyncio.CancelledError:
            raise
        finally:
            code = await proc.wait()
            self._active_proc = None
            self._active_model = None
            self._set_buttons_downloading(False)

        if code == 0:
            self.app.notify(f"Downloaded {name}", timeout=3)
            # Voxtype rewrote ~/.config/voxtype/config.toml during the
            # download; reload our state so the new active model and any
            # other tweaks are reflected in the TUI.
            self.tui.load_state()
            self.refresh_table()
            self._update_disk_label()
        else:
            self.app.notify(
                f"Download failed (exit {code}). Partial file left in place "
                "for re-download.",
                severity="error", timeout=10,
            )

    async def _cancel_active_download(self) -> None:
        proc = self._active_proc
        name = self._active_model
        if proc is None:
            return
        log = self.query_one("#models-log", RichLog)
        log.write("[cancel requested]")
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            log.write("[SIGTERM timed out, sending SIGKILL]")
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass

        # Clean up the partial .bin so the next download starts fresh.
        if name:
            path = model_file_path(self._current_engine(), name)
            if path.exists():
                try:
                    os.unlink(path)
                    log.write(f"[removed partial file {path.name}]")
                except OSError as e:
                    log.write(f"[could not remove partial: {e}]")

        self.app.notify("Download cancelled", severity="warning", timeout=4)
        self._active_proc = None
        self._active_model = None
        self._set_buttons_downloading(False)
        self.refresh_table()
        self._update_disk_label()

    def _set_buttons_downloading(self, downloading: bool) -> None:
        self.query_one("#models-set-active", Button).disabled = downloading
        self.query_one("#models-download", Button).disabled = downloading
        self.query_one("#models-delete", Button).disabled = downloading
        self.query_one("#models-cancel", Button).disabled = not downloading


def _fmt_size(size_bytes: int) -> str:
    mb = size_bytes / (1024 * 1024)
    if mb < 1024:
        return f"{mb:.0f} MB"
    return f"{mb / 1024:.1f} GB"
