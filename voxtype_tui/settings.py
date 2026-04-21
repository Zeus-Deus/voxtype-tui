"""Settings tab — covers engine/model, hotkey, audio, output, GPU, VAD,
post-processing, and remote backend as collapsible sections.

Design notes:
  * Each widget flips `config_dirty` via `AppState.set_setting` on change.
    Global Ctrl+S saves everything atomically; no per-section save.
  * Sections whose fields include a restart-sensitive path get a hint in the
    Collapsible title — the authoritative signal is still the RestartModal
    that appears post-save with the specific fields listed.
  * Widget IDs are prefixed `settings-` to avoid the cross-tab collision that
    bit us on Vocabulary vs Dictionary.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Checkbox, Collapsible, Input, Label, Select, Switch

from . import config

if TYPE_CHECKING:
    from .app import VoxtypeTUI


# --- engine / model catalog -------------------------------------------------

# Values in the TOML `engine` field. Lowercase per voxtype's strict enum.
ENGINES: list[str] = [
    "whisper", "parakeet", "moonshine", "sensevoice",
    "paraformer", "dolphin", "omnilingual",
]

# Known model names per engine, sourced from `voxtype setup --help`. Users who
# point at a local .bin file use the "Custom…" affordance instead.
MODELS_PER_ENGINE: dict[str, list[str]] = {
    "whisper": [
        "tiny", "tiny.en", "base", "base.en",
        "small", "small.en", "medium", "medium.en",
        "large-v3", "large-v3-turbo",
    ],
    "parakeet": ["parakeet-tdt-0.6b-v3", "parakeet-tdt-0.6b-v3-int8"],
    "moonshine": ["tiny", "base"],
    "sensevoice": ["small"],
    "paraformer": [],
    "dolphin": [],
    "omnilingual": [],
}

# Where each engine stores its model name. Voxtype uses a different key for
# parakeet (`model_type`); everything else uses plain `model`.
MODEL_PATH_PER_ENGINE: dict[str, str] = {
    "whisper": "whisper.model",
    "parakeet": "parakeet.model_type",
    "moonshine": "moonshine.model",
    "sensevoice": "sensevoice.model",
    "paraformer": "paraformer.model",
    "dolphin": "dolphin.model",
    "omnilingual": "omnilingual.model",
}

CUSTOM_MODEL = "__custom__"

# Modifier keys offered in the Hotkey section. Right-variants are rare; users
# who need one can hand-edit the TOML.
HOTKEY_MODIFIERS: list[str] = ["LEFTCTRL", "LEFTALT", "LEFTSHIFT", "LEFTMETA"]
HOTKEY_MODES: list[str] = ["push_to_talk", "toggle"]


def engine_section_restart_hint() -> str:
    """Title suffix used for sections that contain restart-sensitive fields."""
    return "  [dim]· restart required[/dim]"


class SettingsPane(VerticalScroll):
    DEFAULT_CSS = """
    SettingsPane { padding: 1 2; }
    SettingsPane Collapsible { margin-bottom: 1; }
    SettingsPane .field-row { height: auto; margin-bottom: 1; align: left middle; }
    SettingsPane .label { width: 18; padding: 1 1 0 0; color: $text-muted; }
    SettingsPane Input { width: 1fr; }
    SettingsPane Select { width: 1fr; }
    SettingsPane Switch { width: auto; }
    SettingsPane .modifier-row { height: 3; padding: 0 0 0 18; }
    SettingsPane .modifier-row Checkbox { margin-right: 2; height: 1; }
    SettingsPane .hidden { display: none; }
    """

    BINDINGS = [
        Binding("escape", "blur", "Blur", show=False),
    ]

    # Guards against feedback loops: when we programmatically update a widget
    # (e.g. after the engine changes, we reset the Model Select), Textual
    # still fires Changed events. This flag lets the handler skip those.
    _suppress_events: bool = False

    def compose(self) -> ComposeResult:
        with Collapsible(
            title=f"Engine & Model{engine_section_restart_hint()}",
            collapsed=False,
            id="settings-engine-section",
        ):
            with Horizontal(classes="field-row"):
                yield Label("Engine", classes="label")
                yield Select(
                    options=[(e, e) for e in ENGINES],
                    allow_blank=False,
                    value=ENGINES[0],
                    id="settings-engine",
                )
            with Horizontal(classes="field-row"):
                yield Label("Model", classes="label")
                yield Select(
                    options=[("(no selection)", Select.BLANK)],
                    id="settings-model",
                )
            with Horizontal(classes="field-row hidden", id="settings-custom-model-row"):
                yield Label("Custom path", classes="label")
                yield Input(
                    placeholder="/path/to/ggml-model.bin",
                    id="settings-custom-model",
                )
            with Horizontal(classes="field-row"):
                yield Label("Language", classes="label")
                yield Input(
                    placeholder="en, auto, de, fr, …",
                    id="settings-language",
                )

        with Collapsible(
            title=f"Hotkey & Activation{engine_section_restart_hint()}",
            collapsed=False,
            id="settings-hotkey-section",
        ):
            with Horizontal(classes="field-row"):
                yield Label("Key", classes="label")
                yield Input(
                    placeholder="SCROLLLOCK, RIGHTALT, F13, …",
                    id="settings-hotkey-key",
                )
            with Horizontal(classes="field-row"):
                yield Label("Modifiers", classes="label")
            with Horizontal(classes="modifier-row"):
                for mod in HOTKEY_MODIFIERS:
                    yield Checkbox(mod, id=f"settings-mod-{mod.lower()}")
            with Horizontal(classes="field-row"):
                yield Label("Mode", classes="label")
                yield Select(
                    options=[(m, m) for m in HOTKEY_MODES],
                    allow_blank=False,
                    value=HOTKEY_MODES[0],
                    id="settings-hotkey-mode",
                )
            with Horizontal(classes="field-row"):
                yield Label("Built-in detect", classes="label")
                yield Switch(value=True, id="settings-hotkey-enabled")

    # --- app/state access ---

    @property
    def tui(self) -> "VoxtypeTUI":
        return self.app  # type: ignore[return-value]

    def _current_engine(self) -> str:
        if self.tui.state is None:
            return "whisper"
        eng = self.tui.state.doc.get("engine")
        return str(eng) if eng else "whisper"

    def _current_model_for(self, engine: str) -> str:
        if self.tui.state is None:
            return ""
        section, key = MODEL_PATH_PER_ENGINE[engine].split(".")
        node = self.tui.state.doc.get(section)
        if node is None:
            return ""
        return str(node.get(key, ""))

    def _model_options(self, engine: str) -> list[tuple[str, str]]:
        known = MODELS_PER_ENGINE.get(engine, [])
        options = [(m, m) for m in known]
        options.append(("Custom path…", CUSTOM_MODEL))
        return options

    # --- lifecycle ---

    def on_mount(self) -> None:
        self.sync_from_state()

    def sync_from_state(self) -> None:
        """Populate all widgets from the current `state.doc`. Called on mount
        and after external reload (ctrl+r)."""
        if self.tui.state is None:
            return
        self._suppress_events = True
        try:
            doc = self.tui.state.doc
            engine = self._current_engine()
            model = self._current_model_for(engine)
            language = str(doc.get("whisper", {}).get("language", ""))

            self.query_one("#settings-engine", Select).value = engine
            self._refresh_model_options(engine, preferred=model)
            self.query_one("#settings-language", Input).value = language

            hotkey = doc.get("hotkey") or {}
            self.query_one("#settings-hotkey-key", Input).value = str(
                hotkey.get("key", "")
            )
            active_modifiers = {str(m) for m in (hotkey.get("modifiers") or [])}
            for mod in HOTKEY_MODIFIERS:
                cb = self.query_one(f"#settings-mod-{mod.lower()}", Checkbox)
                cb.value = mod in active_modifiers
            mode = str(hotkey.get("mode", HOTKEY_MODES[0])) or HOTKEY_MODES[0]
            if mode not in HOTKEY_MODES:
                mode = HOTKEY_MODES[0]
            self.query_one("#settings-hotkey-mode", Select).value = mode
            enabled = hotkey.get("enabled", True)
            self.query_one("#settings-hotkey-enabled", Switch).value = bool(enabled)
        finally:
            self._suppress_events = False

    def _refresh_model_options(self, engine: str, preferred: str) -> None:
        """Rebuild the Model Select for the given engine. If `preferred` is a
        known model for this engine, select it. Otherwise, if it looks like a
        custom path, select Custom… and surface it. Else fall back to the
        engine's first known model (if any)."""
        model_select = self.query_one("#settings-model", Select)
        options = self._model_options(engine)
        known_values = {v for _, v in options}

        model_select.set_options(options)

        custom_row = self.query_one("#settings-custom-model-row", Horizontal)
        custom_input = self.query_one("#settings-custom-model", Input)

        if preferred in known_values:
            model_select.value = preferred
            custom_row.add_class("hidden")
            custom_input.value = ""
        elif preferred:  # non-empty, not a known model → treat as custom path
            model_select.value = CUSTOM_MODEL
            custom_row.remove_class("hidden")
            custom_input.value = preferred
        elif options and options[0][1] != CUSTOM_MODEL:
            model_select.value = options[0][1]
            custom_row.add_class("hidden")
            custom_input.value = ""
        else:
            model_select.value = Select.BLANK
            custom_row.remove_class("hidden")
            custom_input.value = ""

    # --- event handlers ---

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._suppress_events or self.tui.state is None:
            return
        if event.select.id == "settings-engine":
            new_engine = str(event.value)
            old_engine = self._current_engine()
            if new_engine == old_engine:
                return
            self.tui.state.set_setting("engine", new_engine)
            # Preserve current model if it's also valid for the new engine;
            # otherwise pick the new engine's first known model.
            current_model = self._current_model_for(new_engine)
            if not current_model:
                # No model set for new engine yet — carry over from old engine
                # if that name happens to be valid here.
                carry = self._current_model_for(old_engine)
                if carry in MODELS_PER_ENGINE.get(new_engine, []):
                    self._write_model(new_engine, carry)
                    current_model = carry
                else:
                    default = (MODELS_PER_ENGINE.get(new_engine) or [""])[0]
                    if default:
                        self._write_model(new_engine, default)
                        current_model = default
            self._refresh_model_options(new_engine, preferred=current_model)
            self.tui.refresh_dirty()
        elif event.select.id == "settings-model":
            if event.value == Select.BLANK:
                return
            custom_row = self.query_one("#settings-custom-model-row", Horizontal)
            custom_input = self.query_one("#settings-custom-model", Input)
            engine = self._current_engine()
            if event.value == CUSTOM_MODEL:
                custom_row.remove_class("hidden")
                # Only steal focus if the user actually interacted with the
                # Select — never during programmatic hydration, otherwise the
                # next test / user keystroke lands in the Custom input.
                if event.select.has_focus:
                    custom_input.focus()
                # Don't write to config yet — wait for the Input to be filled.
                return
            custom_row.add_class("hidden")
            custom_input.value = ""
            self._write_model(engine, str(event.value))
            self.tui.refresh_dirty()
        elif event.select.id == "settings-hotkey-mode":
            if event.value == Select.BLANK:
                return
            self.tui.state.set_setting("hotkey.mode", str(event.value))
            self.tui.refresh_dirty()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._suppress_events or self.tui.state is None:
            return
        if event.input.id == "settings-custom-model":
            path = event.value.strip()
            if not path:
                return
            self._write_model(self._current_engine(), path)
            self.tui.refresh_dirty()
        elif event.input.id == "settings-language":
            value = event.value.strip()
            if value:
                self.tui.state.set_setting("whisper.language", value)
            else:
                return
            self.tui.refresh_dirty()
        elif event.input.id == "settings-hotkey-key":
            value = event.value.strip()
            if value:
                self.tui.state.set_setting("hotkey.key", value)
                self.tui.refresh_dirty()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if self._suppress_events or self.tui.state is None:
            return
        checkbox_id = event.checkbox.id or ""
        if checkbox_id.startswith("settings-mod-"):
            self._rewrite_modifiers_from_checkboxes()

    def _rewrite_modifiers_from_checkboxes(self) -> None:
        if self.tui.state is None:
            return
        active: list[str] = []
        for mod in HOTKEY_MODIFIERS:
            cb = self.query_one(f"#settings-mod-{mod.lower()}", Checkbox)
            if cb.value:
                active.append(mod)
        self.tui.state.set_setting("hotkey.modifiers", active)
        self.tui.refresh_dirty()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if self._suppress_events or self.tui.state is None:
            return
        if event.switch.id == "settings-hotkey-enabled":
            self.tui.state.set_setting("hotkey.enabled", bool(event.value))
            self.tui.refresh_dirty()

    def _write_model(self, engine: str, model: str) -> None:
        if self.tui.state is None:
            return
        self.tui.state.set_setting(MODEL_PATH_PER_ENGINE[engine], model)

    # --- actions ---

    def action_blur(self) -> None:
        self.app.set_focus(None)
