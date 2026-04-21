"""Tests for Step 3 — manual export (Ctrl+E).

Two layers:

  1. Unit tests for the policy helpers in `voxtype_tui.sync`
     (`default_export_filename`, `default_export_path`, `build_export_bundle`,
     `write_export_bundle`). Covers scope + redact combinations and the
     atomic-write path. Fast, no Pilot harness.

  2. Pilot tests for the `ExportBundleModal` — keybinding, scope/redact
     propagation, success toast, cancel path.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit

from voxtype_tui import sidecar, sync, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.screens.export import ExportBundleModal

from .conftest import FIXTURES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    # The heavily_customized fixture has a remote_api_key and
    # post_process command, which exercises the redact path.
    shutil.copy(FIXTURES / "heavily_customized.toml", cfg)

    async def _inactive():
        return False

    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)
    return cfg, side


def _doc_and_sc_with_secrets() -> tuple[tomlkit.TOMLDocument, sidecar.Sidecar]:
    doc = tomlkit.parse(
        """
engine = "whisper"

[whisper]
model = "tiny"
language = "en"
remote_api_key = "sk-REAL-KEY"

[output]
mode = "type"
pre_output_command = "echo pre"
post_output_command = "echo post"

[output.post_process]
command = "ollama run llama3.2"
timeout_ms = 30000

[text]
[text.replacements]
"cloud code" = "Claude Code"

[hotkey]
key = "F13"

[audio]
device = "default"
"""
    )
    sc = sidecar.Sidecar(
        vocabulary=[sidecar.VocabEntry(phrase="Codemux")],
        replacements=[sidecar.ReplacementEntry(from_text="cloud code")],
    )
    return doc, sc


# ---------------------------------------------------------------------------
# Default path
# ---------------------------------------------------------------------------

def test_default_filename_uses_today() -> None:
    dt = datetime(2026, 4, 21, 15, 30)
    assert sync.default_export_filename(dt) == "voxtype-tui-export-2026-04-21.json"


def test_default_export_path_is_downloads_when_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: ~/Downloads exists → that's the default parent."""
    fake_home = tmp_path / "home"
    downloads = fake_home / "Downloads"
    downloads.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    p = sync.default_export_path()
    assert p.parent == downloads
    assert p.name.startswith("voxtype-tui-export-")
    assert p.name.endswith(".json")


def test_default_export_path_falls_back_to_home_if_no_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback: ~/Downloads missing → suggest home directory. We never
    create ~/Downloads ourselves — respects whatever the user's shell
    setup decided."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    p = sync.default_export_path()
    assert p.parent == fake_home


# ---------------------------------------------------------------------------
# Scope — sync-only vs sync+local
# ---------------------------------------------------------------------------

def test_sync_only_scope_has_empty_local_block() -> None:
    doc, sc = _doc_and_sc_with_secrets()
    bundle = sync.build_export_bundle(
        doc, sc,
        scope=sync.SCOPE_SYNC_ONLY,
        redact_secrets=True,
    )
    assert bundle.local == {}


def test_sync_plus_local_scope_populates_local() -> None:
    doc, sc = _doc_and_sc_with_secrets()
    bundle = sync.build_export_bundle(
        doc, sc,
        scope=sync.SCOPE_SYNC_PLUS_LOCAL,
        redact_secrets=True,
    )
    assert bundle.local != {}
    assert bundle.local["hotkey"]["key"] == "F13"
    assert bundle.local["audio"]["device"] == "default"


def test_build_export_bundle_rejects_unknown_scope() -> None:
    doc, sc = _doc_and_sc_with_secrets()
    with pytest.raises(sync.BundleError):
        sync.build_export_bundle(
            doc, sc, scope="bogus", redact_secrets=True,
        )


# ---------------------------------------------------------------------------
# Redact — blank vs verbatim
# ---------------------------------------------------------------------------

def test_redact_true_blanks_secret_values_but_keeps_paths() -> None:
    doc, sc = _doc_and_sc_with_secrets()
    bundle = sync.build_export_bundle(
        doc, sc,
        scope=sync.SCOPE_SYNC_ONLY,
        redact_secrets=True,
    )
    assert bundle.secrets is not None
    assert bundle.secrets["whisper"]["remote_api_key"] == ""
    assert bundle.secrets["output"]["post_process"]["command"] == ""
    assert bundle.secrets["output"]["pre_output_command"] == ""
    assert bundle.secrets["output"]["post_output_command"] == ""

    # And the raw text of the exported file genuinely doesn't contain
    # the real secrets — regex-scan as a belt-and-suspenders check.
    text = sync.to_json(bundle)
    assert "sk-REAL-KEY" not in text
    assert "ollama run" not in text
    assert "echo pre" not in text
    assert "echo post" not in text


def test_redact_false_includes_secret_values_verbatim() -> None:
    doc, sc = _doc_and_sc_with_secrets()
    bundle = sync.build_export_bundle(
        doc, sc,
        scope=sync.SCOPE_SYNC_ONLY,
        redact_secrets=False,
    )
    assert bundle.secrets is not None
    assert bundle.secrets["whisper"]["remote_api_key"] == "sk-REAL-KEY"
    assert bundle.secrets["output"]["post_process"]["command"] == "ollama run llama3.2"
    assert bundle.secrets["output"]["pre_output_command"] == "echo pre"


def test_export_bundle_sync_block_never_contains_secrets_regardless_of_redact() -> None:
    """Invariant: even `redact_secrets=False` keeps the SYNC block
    secret-free. Secrets live in their own block so the presence of
    `secrets` is the single "sensitive content" signal."""
    doc, sc = _doc_and_sc_with_secrets()
    for redact in (True, False):
        bundle = sync.build_export_bundle(
            doc, sc,
            scope=sync.SCOPE_SYNC_ONLY,
            redact_secrets=redact,
        )
        settings = bundle.sync["settings"]
        assert "remote_api_key" not in settings.get("whisper", {})
        assert "pre_output_command" not in settings.get("output", {})
        assert "post_output_command" not in settings.get("output", {})
        assert "command" not in settings.get("output", {}).get("post_process", {})


def test_redact_secrets_dict_only_blanks_paths_that_existed() -> None:
    """No secrets present → redact_secrets_dict returns empty, doesn't
    synthesize blank paths the user never set."""
    assert sync.redact_secrets_dict({}) == {}
    assert sync.redact_secrets_dict({"whisper": {}}) == {}
    # Partially set: only `remote_api_key` present → only that key blanked.
    out = sync.redact_secrets_dict({"whisper": {"remote_api_key": "sk-x"}})
    assert out == {"whisper": {"remote_api_key": ""}}


def test_export_bundle_without_secrets_in_config_emits_empty_secrets_block() -> None:
    """Config has no secret fields → exported bundle has a `secrets: {}`
    block (present, empty) rather than no block at all. That way the
    file is visibly flagged as produced by the manual export dialog."""
    doc = tomlkit.parse(
        '[whisper]\nmodel = "tiny"\n[text]\n[text.replacements]\n"a" = "b"\n'
    )
    sc = sidecar.Sidecar(
        vocabulary=[],
        replacements=[sidecar.ReplacementEntry(from_text="a")],
    )
    bundle = sync.build_export_bundle(
        doc, sc,
        scope=sync.SCOPE_SYNC_ONLY,
        redact_secrets=True,
    )
    assert bundle.secrets == {}


# ---------------------------------------------------------------------------
# write_export_bundle — atomic write
# ---------------------------------------------------------------------------

def test_write_export_creates_file(tmp_path: Path) -> None:
    doc, sc = _doc_and_sc_with_secrets()
    bundle = sync.build_export_bundle(
        doc, sc, scope=sync.SCOPE_SYNC_ONLY, redact_secrets=True,
    )
    target = tmp_path / "out.json"
    written = sync.write_export_bundle(bundle, target)
    assert written == target
    data = json.loads(target.read_text())
    assert data["format"] == sync.FORMAT_TAG


def test_write_export_expands_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """User types `~/something.json` into the target Input. Must resolve
    relative to HOME, not be treated as a literal `~` directory."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    doc, sc = _doc_and_sc_with_secrets()
    bundle = sync.build_export_bundle(
        doc, sc, scope=sync.SCOPE_SYNC_ONLY, redact_secrets=True,
    )
    written = sync.write_export_bundle(bundle, Path("~/exported.json"))
    assert written == fake_home / "exported.json"
    assert written.exists()


def test_write_export_creates_single_missing_leaf_directory(tmp_path: Path) -> None:
    """Quality-of-life: if parent is missing BUT grandparent exists,
    create the leaf dir. Gives the user "I want to dump exports in
    ~/Downloads/voxtype-exports/" without pre-creating."""
    target = tmp_path / "exports" / "bundle.json"
    doc, sc = _doc_and_sc_with_secrets()
    bundle = sync.build_export_bundle(
        doc, sc, scope=sync.SCOPE_SYNC_ONLY, redact_secrets=True,
    )
    written = sync.write_export_bundle(bundle, target)
    assert written.exists()


def test_write_export_refuses_deeply_missing_path(tmp_path: Path) -> None:
    """Typo-guard: multiple missing directory levels almost certainly
    means the user mistyped. Fail loud instead of creating a hierarchy
    they didn't ask for."""
    deep = tmp_path / "a" / "b" / "c" / "file.json"
    doc, sc = _doc_and_sc_with_secrets()
    bundle = sync.build_export_bundle(
        doc, sc, scope=sync.SCOPE_SYNC_ONLY, redact_secrets=True,
    )
    with pytest.raises(FileNotFoundError):
        sync.write_export_bundle(bundle, deep)


def test_write_export_is_atomic_on_replace_failure(tmp_path: Path) -> None:
    """Mock `os.replace` to fail → target unchanged, no tempfile leak."""
    target = tmp_path / "out.json"
    target.write_text("PRIOR")
    doc, sc = _doc_and_sc_with_secrets()
    bundle = sync.build_export_bundle(
        doc, sc, scope=sync.SCOPE_SYNC_ONLY, redact_secrets=True,
    )

    def boom(src, dst):
        raise OSError("simulated")

    with patch("voxtype_tui.sync.os.replace", side_effect=boom):
        with pytest.raises(OSError):
            sync.write_export_bundle(bundle, target)

    assert target.read_text() == "PRIOR"
    leftover = list(tmp_path.glob("out.json.*.tmp"))
    assert leftover == []


# ---------------------------------------------------------------------------
# Pilot tests — modal lifecycle
# ---------------------------------------------------------------------------

async def test_ctrl_e_opens_export_modal(tmp_env) -> None:
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.state is not None
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert isinstance(app.screen, ExportBundleModal)


async def test_ctrl_e_ignored_while_typing_in_input(tmp_env) -> None:
    """check_action guard: don't open the modal while user is typing —
    that would interrupt their edit and be infuriating."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Focus the Vocabulary tab's "add" Input via the `n` binding.
        await pilot.press("n")
        await pilot.pause()
        from textual.widgets import Input
        assert isinstance(app.focused, Input)
        await pilot.press("ctrl+e")
        await pilot.pause()
        # Still on the main screen — modal didn't open.
        assert not isinstance(app.screen, ExportBundleModal)


async def test_export_writes_file_and_toasts(tmp_env, tmp_path: Path) -> None:
    cfg, side = tmp_env
    target = tmp_path / "out.json"
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ExportBundleModal)

        # Replace the target path with our tmp target.
        from textual.widgets import Input
        path_input = modal.query_one("#export-path", Input)
        path_input.value = str(target)
        await pilot.pause()

        # Click Export.
        await pilot.click("#do-export")
        await pilot.pause()

    assert target.exists()
    data = json.loads(target.read_text())
    assert data["format"] == sync.FORMAT_TAG
    # Default modal state is Sync-only + Redact → no local, secrets blanked.
    assert data["local"] == {}
    # heavily_customized fixture has a post_process.command plus the two
    # wrapper *_command fields — redaction should blank each of them,
    # preserving the paths so the file visibly reports "these were
    # exported as redacted".
    assert data["secrets"]["output"]["post_process"]["command"] == ""
    assert data["secrets"]["output"]["pre_output_command"] == ""
    assert data["secrets"]["output"]["post_output_command"] == ""
    raw = target.read_text()
    assert "ollama run" not in raw
    assert "hyprctl" not in raw


async def test_export_cancel_does_not_write(tmp_env, tmp_path: Path) -> None:
    cfg, side = tmp_env
    target = tmp_path / "out.json"
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ExportBundleModal)

        from textual.widgets import Input
        modal.query_one("#export-path", Input).value = str(target)
        await pilot.pause()

        await pilot.click("#do-cancel")
        await pilot.pause()

    assert not target.exists()


async def test_export_plus_local_scope_produces_local_block(
    tmp_env, tmp_path: Path,
) -> None:
    cfg, side = tmp_env
    target = tmp_path / "full-backup.json"
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ExportBundleModal)

        from textual.widgets import Input, RadioButton
        modal.query_one("#export-path", Input).value = str(target)
        # Click the sync+local radio.
        modal.query_one("#scope-sync-local", RadioButton).value = True
        await pilot.pause()

        await pilot.click("#do-export")
        await pilot.pause()

    data = json.loads(target.read_text())
    assert data["local"]["hotkey"]["key"] == "RIGHTALT"


async def test_export_redact_off_includes_secrets(
    tmp_env, tmp_path: Path,
) -> None:
    cfg, side = tmp_env
    target = tmp_path / "with-secrets.json"
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ExportBundleModal)

        from textual.widgets import Checkbox, Input
        modal.query_one("#export-path", Input).value = str(target)
        modal.query_one("#export-redact", Checkbox).value = False
        await pilot.pause()

        await pilot.click("#do-export")
        await pilot.pause()

    # heavily_customized has a real post_process.command — present
    # verbatim when redact is off.
    raw = target.read_text()
    assert "ollama run" in raw


async def test_export_error_on_bad_path_stays_in_modal(tmp_env) -> None:
    """Typing a deeply-missing path shows an error label; the modal
    doesn't dismiss so the user can fix the path without reopening."""
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ExportBundleModal)

        from textual.widgets import Input, Label
        modal.query_one("#export-path", Input).value = "/no/such/deep/tree/x.json"
        await pilot.pause()

        await pilot.click("#do-export")
        await pilot.pause()

        # Still on the modal.
        assert isinstance(app.screen, ExportBundleModal)
        err = modal.query_one("#error", Label)
        assert "hidden" not in err.classes
