"""Tests for Step 4 — manual import (Ctrl+I) and Vexis adapter wiring.

Unit tests cover the pure import pipeline (`load_bundle_file`,
`diff_bundle_against_state`, `apply_bundle_to_state`). Pilot tests cover
the modal lifecycle: load, preview, apply, cancel.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import tomlkit

from voxtype_tui import sidecar, sync, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.screens.import_bundle import ImportBundleModal
from voxtype_tui.state import AppState

from .conftest import FIXTURES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)

    async def _inactive():
        return False

    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)
    return cfg, side


def _stock_state(tmp_env) -> AppState:
    cfg, side = tmp_env
    return AppState.load(cfg, side)


def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2))


def _make_voxtype_bundle(
    *,
    vocab: list[str] | None = None,
    replacements: list[tuple[str, str, str]] | None = None,
    settings: dict | None = None,
    secrets: dict | None = None,
    schema_version: int = 1,
) -> dict:
    vocab_list = [{"phrase": v} for v in (vocab or [])]
    rep_list = [
        {"from": f, "to": t, "category": c}
        for f, t, c in (replacements or [])
    ]
    bundle: dict = {
        "schema_version": schema_version,
        "format": sync.FORMAT_TAG,
        "generated_at": "2026-04-21T20:00:00Z",
        "generated_by_device": "test-device",
        "local_sync_hash": "0" * 64,
        "sync": {
            "vocabulary": vocab_list,
            "replacements": rep_list,
            "settings": settings or {},
        },
        "local": {},
    }
    if secrets is not None:
        bundle["secrets"] = secrets
    return bundle


# ---------------------------------------------------------------------------
# load_bundle_file — format detection + routing
# ---------------------------------------------------------------------------

def test_load_native_bundle(tmp_path: Path) -> None:
    path = tmp_path / "native.json"
    _write_json(path, _make_voxtype_bundle(vocab=["Alpha"]))
    bundle, warnings = sync.load_bundle_file(path)
    assert bundle.format == sync.FORMAT_TAG
    assert [v["phrase"] for v in bundle.sync["vocabulary"]] == ["Alpha"]
    assert warnings == []


def test_load_vexis_dictionary_migrates_command(tmp_path: Path) -> None:
    vexis_path = tmp_path / "dict.json"
    shutil.copy(FIXTURES / "vexis_dictionary_sample.json", vexis_path)
    bundle, warnings = sync.load_bundle_file(vexis_path)
    # Wrapped into a synthetic voxtype-tui bundle.
    assert bundle.format == sync.FORMAT_TAG
    assert bundle.generated_by_device == sync.VEXIS_DICTIONARY_FORMAT
    # Fixture has a `command`-category row; it must land as Replacement.
    by_from = {r["from"]: r for r in bundle.sync["replacements"]}
    assert by_from["slash codemux release"]["category"] == "Replacement"
    assert warnings and "Vexis dictionary" in warnings[0]


def test_load_vexis_vocabulary_strings(tmp_path: Path) -> None:
    path = tmp_path / "vocab-strings.json"
    shutil.copy(FIXTURES / "vexis_vocabulary_sample_strings.json", path)
    bundle, warnings = sync.load_bundle_file(path)
    assert bundle.generated_by_device == sync.VEXIS_VOCABULARY_FORMAT
    phrases = [v["phrase"] for v in bundle.sync["vocabulary"]]
    assert "Codemux" in phrases
    assert warnings and "Vexis vocabulary" in warnings[0]


def test_load_vexis_vocabulary_objects(tmp_path: Path) -> None:
    path = tmp_path / "vocab-objects.json"
    shutil.copy(FIXTURES / "vexis_vocabulary_sample_objects.json", path)
    bundle, warnings = sync.load_bundle_file(path)
    phrases = [v["phrase"] for v in bundle.sync["vocabulary"]]
    # Case-insensitive dedup: "Codemux" + "codemux" → one entry.
    lowered = [p.lower() for p in phrases]
    assert len(phrases) == len(set(lowered))


def test_load_schema_too_new_rejects(tmp_path: Path) -> None:
    path = tmp_path / "future.json"
    _write_json(path, _make_voxtype_bundle(schema_version=99))
    with pytest.raises(sync.BundleError, match="newer"):
        sync.load_bundle_file(path)


def test_load_invalid_json_rejects(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{ nope }")
    with pytest.raises(sync.BundleError, match="invalid JSON"):
        sync.load_bundle_file(path)


def test_load_oversize_rejects(tmp_path: Path) -> None:
    path = tmp_path / "huge.json"
    path.write_text("x" * (sync.MAX_BUNDLE_BYTES + 1))
    with pytest.raises(sync.BundleError, match="limit"):
        sync.load_bundle_file(path)


def test_load_unknown_shape_rejects(tmp_path: Path) -> None:
    path = tmp_path / "weird.json"
    path.write_text(json.dumps({"some": "random", "object": True}))
    with pytest.raises(sync.BundleError, match="[Uu]nrecognized"):
        sync.load_bundle_file(path)


def test_load_missing_file_rejects(tmp_path: Path) -> None:
    with pytest.raises(sync.BundleError):
        sync.load_bundle_file(tmp_path / "does-not-exist.json")


# ---------------------------------------------------------------------------
# diff_bundle_against_state
# ---------------------------------------------------------------------------

def test_diff_vocab_splits_added_vs_unchanged(tmp_env) -> None:
    state = _stock_state(tmp_env)
    # Start with an existing vocab.
    state.add_vocab("Codemux")
    bundle_dict = _make_voxtype_bundle(vocab=["Codemux", "NewWord"])
    bundle = sync.from_json(json.dumps(bundle_dict))

    preview = sync.diff_bundle_against_state(bundle, state.doc, state.sc)
    assert preview.vocab.added == ["NewWord"]
    assert preview.vocab.unchanged == ["Codemux"]


def test_diff_replacements_splits_added_updated_unchanged(tmp_env) -> None:
    state = _stock_state(tmp_env)
    state.upsert_replacement("keep me", "same value", "Replacement")
    state.upsert_replacement("update me", "old value", "Replacement")

    bundle_dict = _make_voxtype_bundle(replacements=[
        ("keep me", "same value", "Replacement"),        # unchanged
        ("update me", "new value", "Replacement"),        # updated
        ("brand new", "fresh", "Replacement"),            # added
    ])
    bundle = sync.from_json(json.dumps(bundle_dict))
    preview = sync.diff_bundle_against_state(bundle, state.doc, state.sc)

    assert preview.replacements.added == [("brand new", "fresh")]
    assert preview.replacements.updated == [("update me", "old value", "new value")]
    assert preview.replacements.unchanged == ["keep me"]


def test_diff_settings_flags_dangerous_paths(tmp_env) -> None:
    state = _stock_state(tmp_env)
    # Bundle changes remote_endpoint (dangerous) + plain model (not).
    bundle_dict = _make_voxtype_bundle(
        settings={
            "whisper": {
                "model": "tiny",
                "remote_endpoint": "http://evil.example.com",
            }
        },
    )
    bundle = sync.from_json(json.dumps(bundle_dict))
    preview = sync.diff_bundle_against_state(bundle, state.doc, state.sc)

    paths = {c.path: c for c in preview.settings}
    assert "whisper.remote_endpoint" in paths
    assert paths["whisper.remote_endpoint"].dangerous is True
    # Model is portable, not a dangerous path.
    if "whisper.model" in paths:
        assert paths["whisper.model"].dangerous is False


def test_diff_secrets_redacted_import_is_no_op(tmp_env) -> None:
    """A bundle whose secrets block has an empty `remote_api_key`
    (manual export with Redact=True) must NOT propose overwriting a
    real local value with an empty string."""
    state = _stock_state(tmp_env)
    state.set_setting("whisper.remote_api_key", "sk-LOCAL")

    bundle_dict = _make_voxtype_bundle(
        secrets={"whisper": {"remote_api_key": ""}},
    )
    bundle = sync.from_json(json.dumps(bundle_dict))
    preview = sync.diff_bundle_against_state(bundle, state.doc, state.sc)

    paths = {c.path for c in preview.settings}
    assert "whisper.remote_api_key" not in paths


def test_diff_include_local_controls_local_block_visibility(tmp_env) -> None:
    state = _stock_state(tmp_env)
    bundle_dict = _make_voxtype_bundle()
    bundle_dict["local"] = {"hotkey": {"key": "F20"}}
    bundle = sync.from_json(json.dumps(bundle_dict))

    # Default: local block is ignored.
    preview = sync.diff_bundle_against_state(bundle, state.doc, state.sc)
    assert all("hotkey.key" not in c.path for c in preview.settings)

    # Opt-in: local block surfaces.
    preview_with_local = sync.diff_bundle_against_state(
        bundle, state.doc, state.sc, include_local=True,
    )
    assert any("hotkey.key" in c.path for c in preview_with_local.settings)


# ---------------------------------------------------------------------------
# apply_bundle_to_state
# ---------------------------------------------------------------------------

def test_apply_vocab_appends_new_phrases_only(tmp_env) -> None:
    state = _stock_state(tmp_env)
    state.add_vocab("Existing")

    bundle_dict = _make_voxtype_bundle(vocab=["Existing", "Fresh"])
    bundle = sync.from_json(json.dumps(bundle_dict))
    sync.apply_bundle_to_state(bundle, state.doc, state.sc)

    phrases = [v.phrase for v in state.sc.vocabulary]
    # Existing preserved; Fresh appended; no duplicates.
    assert phrases.count("Existing") == 1
    assert phrases.count("Fresh") == 1
    # initial_prompt reflects the merged order.
    from voxtype_tui import config
    ip = config.get_initial_prompt(state.doc)
    assert ip is not None
    assert "Existing" in ip
    assert "Fresh" in ip


def test_apply_replacements_upsert_and_keep_existing(tmp_env) -> None:
    state = _stock_state(tmp_env)
    state.upsert_replacement("keep me", "retained", "Replacement")

    bundle_dict = _make_voxtype_bundle(replacements=[
        ("new rep", "fresh", "Replacement"),
        ("keep me", "retained", "Replacement"),
    ])
    bundle = sync.from_json(json.dumps(bundle_dict))
    sync.apply_bundle_to_state(bundle, state.doc, state.sc)

    from voxtype_tui import config
    reps = config.get_replacements(state.doc)
    assert reps["keep me"] == "retained"
    assert reps["new rep"] == "fresh"


def test_apply_does_not_remove_replacements_not_in_bundle(tmp_env) -> None:
    """Merge, not replace: existing keys that the import doesn't
    mention must survive."""
    state = _stock_state(tmp_env)
    state.upsert_replacement("local only", "stays", "Replacement")

    bundle_dict = _make_voxtype_bundle(
        replacements=[("imported", "value", "Replacement")],
    )
    bundle = sync.from_json(json.dumps(bundle_dict))
    sync.apply_bundle_to_state(bundle, state.doc, state.sc)

    from voxtype_tui import config
    reps = config.get_replacements(state.doc)
    assert "local only" in reps
    assert "imported" in reps


def test_apply_settings_overwrites_fields(tmp_env) -> None:
    state = _stock_state(tmp_env)
    bundle_dict = _make_voxtype_bundle(
        settings={
            "whisper": {"model": "tiny", "language": "de"},
            "text": {"spoken_punctuation": True},
        },
    )
    bundle = sync.from_json(json.dumps(bundle_dict))
    sync.apply_bundle_to_state(bundle, state.doc, state.sc)

    assert str(state.doc["whisper"]["model"]) == "tiny"
    assert str(state.doc["whisper"]["language"]) == "de"
    assert bool(state.doc["text"]["spoken_punctuation"]) is True


def test_apply_secrets_skips_blank(tmp_env) -> None:
    """Redacted import values (empty strings) must not clobber real
    local secrets."""
    state = _stock_state(tmp_env)
    state.set_setting("whisper.remote_api_key", "sk-LOCAL")

    bundle_dict = _make_voxtype_bundle(
        secrets={"whisper": {"remote_api_key": ""}},
    )
    bundle = sync.from_json(json.dumps(bundle_dict))
    sync.apply_bundle_to_state(bundle, state.doc, state.sc)

    assert str(state.doc["whisper"]["remote_api_key"]) == "sk-LOCAL"


def test_apply_secrets_overwrites_when_non_blank(tmp_env) -> None:
    state = _stock_state(tmp_env)
    state.set_setting("whisper.remote_api_key", "sk-OLD")

    bundle_dict = _make_voxtype_bundle(
        secrets={"whisper": {"remote_api_key": "sk-NEW"}},
    )
    bundle = sync.from_json(json.dumps(bundle_dict))
    sync.apply_bundle_to_state(bundle, state.doc, state.sc)

    assert str(state.doc["whisper"]["remote_api_key"]) == "sk-NEW"


def test_apply_include_local_gated_by_flag(tmp_env) -> None:
    state = _stock_state(tmp_env)
    original_key = str(state.doc.get("hotkey", {}).get("key", ""))

    bundle_dict = _make_voxtype_bundle()
    bundle_dict["local"] = {"hotkey": {"key": "F20"}}
    bundle = sync.from_json(json.dumps(bundle_dict))

    # Default: local ignored → hotkey unchanged.
    sync.apply_bundle_to_state(bundle, state.doc, state.sc)
    assert str(state.doc["hotkey"]["key"]) == original_key

    # Opt-in: local applied.
    sync.apply_bundle_to_state(bundle, state.doc, state.sc, include_local=True)
    assert str(state.doc["hotkey"]["key"]) == "F20"


def test_apply_over_token_limit_warns(tmp_env) -> None:
    state = _stock_state(tmp_env)
    big = [f"phrase-{i:04d}" for i in range(120)]
    bundle_dict = _make_voxtype_bundle(vocab=big)
    bundle = sync.from_json(json.dumps(bundle_dict))
    warnings = sync.apply_bundle_to_state(bundle, state.doc, state.sc)
    assert warnings, "expected a token-limit warning"


def test_apply_vexis_dictionary_end_to_end(tmp_env, tmp_path: Path) -> None:
    path = tmp_path / "vexis.json"
    shutil.copy(FIXTURES / "vexis_dictionary_sample.json", path)
    state = _stock_state(tmp_env)
    bundle, _warnings = sync.load_bundle_file(path)

    sync.apply_bundle_to_state(bundle, state.doc, state.sc)

    from voxtype_tui import config
    reps = config.get_replacements(state.doc)
    # Fixture's "command" row ends up as a replacement.
    assert reps["slash codemux release"] == "/codemux-release"
    # And the Capitalization row.
    assert reps["type script"] == "TypeScript"
    cats = {r.from_text: r.category for r in state.sc.replacements}
    assert cats["slash codemux release"] == "Replacement"
    assert cats["type script"] == "Capitalization"


def test_apply_vexis_vocabulary_end_to_end(tmp_env, tmp_path: Path) -> None:
    path = tmp_path / "vexis-vocab.json"
    shutil.copy(FIXTURES / "vexis_vocabulary_sample_strings.json", path)
    state = _stock_state(tmp_env)
    bundle, _warnings = sync.load_bundle_file(path)

    sync.apply_bundle_to_state(bundle, state.doc, state.sc)

    phrases = [v.phrase for v in state.sc.vocabulary]
    assert "Codemux" in phrases
    assert "Claude Code" in phrases


# ---------------------------------------------------------------------------
# Pilot tests — modal
# ---------------------------------------------------------------------------

async def test_ctrl_i_opens_import_modal(tmp_env) -> None:
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+i")
        await pilot.pause()
        assert isinstance(app.screen, ImportBundleModal)


async def test_ctrl_i_ignored_in_input(tmp_env) -> None:
    cfg, side = tmp_env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")  # focus vocab-add Input
        await pilot.pause()
        from textual.widgets import Input
        assert isinstance(app.focused, Input)
        await pilot.press("ctrl+i")
        await pilot.pause()
        assert not isinstance(app.screen, ImportBundleModal)


async def test_import_load_and_apply_merges_into_shadow(
    tmp_env, tmp_path: Path,
) -> None:
    cfg, side = tmp_env
    src = tmp_path / "to-import.json"
    _write_json(src, _make_voxtype_bundle(
        vocab=["Merged"],
        replacements=[("slashthing", "/thing", "Replacement")],
    ))
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+i")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ImportBundleModal)

        from textual.widgets import Input
        modal.query_one("#import-path", Input).value = str(src)
        await pilot.pause()
        await pilot.click("#do-load")
        await pilot.pause()

        # Preview surfaces.
        assert not modal.query_one("#preview-scroll").has_class("hidden")

        # Apply.
        await pilot.click("#do-apply")
        await pilot.pause()

        assert not isinstance(app.screen, ImportBundleModal)
        # Shadow mutated but NOT saved.
        assert any(v.phrase == "Merged" for v in app.state.sc.vocabulary)
        from voxtype_tui import config
        assert config.get_replacements(app.state.doc).get("slashthing") == "/thing"
        assert app.state.dirty is True
        # File on disk unchanged until user hits Ctrl+S.
        assert "Merged" not in cfg.read_text()


async def test_import_cancel_leaves_shadow_untouched(
    tmp_env, tmp_path: Path,
) -> None:
    cfg, side = tmp_env
    src = tmp_path / "to-import.json"
    _write_json(src, _make_voxtype_bundle(vocab=["ShouldNotLand"]))
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+i")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ImportBundleModal)

        from textual.widgets import Input
        modal.query_one("#import-path", Input).value = str(src)
        await pilot.pause()
        await pilot.click("#do-load")
        await pilot.pause()

        await pilot.click("#do-cancel")
        await pilot.pause()

        assert all(
            v.phrase != "ShouldNotLand" for v in app.state.sc.vocabulary
        )


async def test_import_error_shown_in_modal_on_bad_file(tmp_env, tmp_path) -> None:
    cfg, side = tmp_env
    bad = tmp_path / "bad.json"
    bad.write_text("{ broken ")
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+i")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ImportBundleModal)

        from textual.widgets import Input, Label
        modal.query_one("#import-path", Input).value = str(bad)
        await pilot.pause()
        await pilot.click("#do-load")
        await pilot.pause()

        # Modal still up, error label visible, Apply disabled.
        assert isinstance(app.screen, ImportBundleModal)
        err = modal.query_one("#error", Label)
        assert "hidden" not in err.classes
        from textual.widgets import Button
        assert modal.query_one("#do-apply", Button).disabled is True
