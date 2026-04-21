"""Round-trip tests: for each fixture × mutation, assert no data loss,
comments preserved, only intended scope changed, and the result is still
valid per Voxtype's own parser.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest
import tomlkit

from voxtype_tui import config
from .conftest import (
    comment_lines,
    count_comment_lines,
    flatten,
    load_toml,
    validates_with_voxtype,
)


def _set_nested(doc, path: str, value) -> None:
    parts = path.split(".")
    node = doc
    for p in parts[:-1]:
        if p not in node:
            node[p] = tomlkit.table()
        node = node[p]
    node[parts[-1]] = value


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def test_fixture_validates_before_any_change(fixture_path: Path) -> None:
    ok, stderr = validates_with_voxtype(fixture_path)
    assert ok, f"{fixture_path.name} rejected at rest: {stderr}"


# ---------------------------------------------------------------------------
# Round-trip for supported mutations
# ---------------------------------------------------------------------------

# Each entry: (name, mutate_fn, clears_section)
# clears_section=True → the mutation deletes a whole section, so comments
# living inside that section are expected to disappear. The comment-
# preservation test skips these; the still-valid/read-back tests do not.
MUTATIONS: list[tuple[str, Callable[[object], None], bool]] = [
    ("add_vocab",
     lambda d: config.set_initial_prompt(d, "Foo, Bar, Baz"), False),
    ("replace_vocab",
     lambda d: config.set_initial_prompt(d, "Alpha, Beta"), False),
    ("clear_vocab",
     lambda d: config.set_initial_prompt(d, None), False),
    ("add_replacement",
     lambda d: config.add_replacement(d, "cloud code", "Claude Code"), False),
    ("multi_replacement",
     lambda d: config.set_replacements(d, {
         "cloud code": "Claude Code",
         "vox type": "voxtype",
         "slash codemux release": "/codemux-release",
     }), False),
    ("clear_replacements",
     lambda d: config.set_replacements(d, {}), True),
    ("change_model",
     lambda d: _set_nested(d, "whisper.model", "tiny.en"), False),
    ("change_language",
     lambda d: _set_nested(d, "whisper.language", "de"), False),
    ("change_hotkey",
     lambda d: _set_nested(d, "hotkey.key", "F13"), False),
]

MUTATION_IDS = [m[0] for m in MUTATIONS]


@pytest.mark.parametrize("mutation_name,mutate,clears_section", MUTATIONS, ids=MUTATION_IDS)
def test_mutation_preserves_comments(
    fixture_path: Path, mutation_name: str, mutate, clears_section: bool
) -> None:
    if clears_section:
        pytest.skip("mutation clears a section; comments within it legitimately removed")
    orig = fixture_path.read_text()
    orig_comments = comment_lines(orig)

    doc = config.load(fixture_path)
    mutate(doc)
    config.save_atomic(doc, fixture_path)

    new = fixture_path.read_text()
    new_set = set(comment_lines(new))
    missing = [c for c in orig_comments if c not in new_set]
    assert not missing, (
        f"lost {len(missing)} comment line(s) after {mutation_name}: "
        f"{missing[:5]}"
    )


@pytest.mark.parametrize("mutation_name,mutate,clears_section", MUTATIONS, ids=MUTATION_IDS)
def test_mutation_still_validates(
    fixture_path: Path, mutation_name: str, mutate, clears_section: bool
) -> None:
    doc = config.load(fixture_path)
    mutate(doc)
    config.save_atomic(doc, fixture_path)
    ok, stderr = validates_with_voxtype(fixture_path)
    assert ok, f"{mutation_name} on {fixture_path.name} broke validation: {stderr}"


@pytest.mark.parametrize("mutation_name,mutate,clears_section", MUTATIONS, ids=MUTATION_IDS)
def test_mutation_read_back_matches(
    fixture_path: Path, mutation_name: str, mutate, clears_section: bool
) -> None:
    doc = config.load(fixture_path)
    mutate(doc)
    expected = tomlkit.dumps(doc)
    config.save_atomic(doc, fixture_path)
    reread = fixture_path.read_text()
    # The write goes through tomlkit.dumps(doc) which is what we captured.
    assert reread == expected, (
        f"{mutation_name}: on-disk form differs from in-memory dump"
    )


# ---------------------------------------------------------------------------
# Scope assertions — only the intended path(s) should change
# ---------------------------------------------------------------------------

def _diff_flat(old: dict, new: dict) -> dict:
    added = {k: new[k] for k in new.keys() - old.keys()}
    removed = {k: old[k] for k in old.keys() - new.keys()}
    changed = {k: (old[k], new[k]) for k in old.keys() & new.keys() if old[k] != new[k]}
    return {"added": added, "removed": removed, "changed": changed}


def test_add_vocab_touches_only_initial_prompt(fixture_path: Path) -> None:
    before = flatten(config.load(fixture_path))
    doc = config.load(fixture_path)
    config.set_initial_prompt(doc, "One, Two, Three")
    config.save_atomic(doc, fixture_path)
    after = flatten(config.load(fixture_path))

    diff = _diff_flat(before, after)
    all_touched = (
        set(diff["added"]) | set(diff["removed"]) | set(diff["changed"])
    )
    assert all_touched == {"whisper.initial_prompt"}, (
        f"unexpected scope: {all_touched}"
    )


def test_add_replacement_touches_only_text_replacements(fixture_path: Path) -> None:
    before = flatten(config.load(fixture_path))
    doc = config.load(fixture_path)
    config.set_replacements(doc, {"cloud code": "Claude Code"})
    config.save_atomic(doc, fixture_path)
    after = flatten(config.load(fixture_path))

    diff = _diff_flat(before, after)
    all_touched = (
        set(diff["added"]) | set(diff["removed"]) | set(diff["changed"])
    )
    assert all(p.startswith("text.replacements") for p in all_touched), (
        f"add_replacement escaped its scope: {all_touched}"
    )


def test_change_model_only_touches_whisper_model(fixture_path: Path) -> None:
    before = flatten(config.load(fixture_path))
    doc = config.load(fixture_path)
    current = before.get("whisper.model")
    new_model = "tiny.en" if current != "tiny.en" else "base.en"
    _set_nested(doc, "whisper.model", new_model)
    config.save_atomic(doc, fixture_path)
    after = flatten(config.load(fixture_path))

    diff = _diff_flat(before, after)
    all_touched = (
        set(diff["added"]) | set(diff["removed"]) | set(diff["changed"])
    )
    assert all_touched == {"whisper.model"}, f"unexpected scope: {all_touched}"


# ---------------------------------------------------------------------------
# Restart-sensitive detection
# ---------------------------------------------------------------------------

def test_vocab_change_is_restart_sensitive(fixture_path: Path) -> None:
    """Adding or changing whisper.initial_prompt must flag as restart-
    sensitive. Empirically the daemon caches the text layer at startup — if
    we say 'saved' and don't prompt for restart, the user's new vocab words
    silently have no effect on the next transcription."""
    before = config.load(fixture_path)
    after = config.load(fixture_path)
    config.set_initial_prompt(after, "Hello, World")
    assert "whisper.initial_prompt" in config.diff_restart_sensitive(before, after)


def test_replacement_add_is_restart_sensitive(fixture_path: Path) -> None:
    before = config.load(fixture_path)
    after = config.load(fixture_path)
    config.add_replacement(after, "slash deploy", "/deploy")
    assert "text.replacements" in config.diff_restart_sensitive(before, after)


def test_replacement_remove_is_restart_sensitive(fixture_path: Path) -> None:
    before = config.load(fixture_path)
    # Seed a replacement we can then remove
    config.add_replacement(before, "temp one", "temp")
    config.save_atomic(before, fixture_path)
    before = config.load(fixture_path)
    after = config.load(fixture_path)
    config.remove_replacement(after, "temp one")
    assert "text.replacements" in config.diff_restart_sensitive(before, after)


def test_spoken_punctuation_toggle_is_restart_sensitive(fixture_path: Path) -> None:
    # Flip whichever boolean is in the fixture, so the diff is non-empty
    before = config.load(fixture_path)
    current = config._get_in(before, "text.spoken_punctuation")
    new_val = False if bool(current) else True
    after = config.load(fixture_path)
    _set_nested(after, "text.spoken_punctuation", new_val)
    assert "text.spoken_punctuation" in config.diff_restart_sensitive(before, after)


def test_smart_auto_submit_toggle_is_restart_sensitive(fixture_path: Path) -> None:
    before = config.load(fixture_path)
    current = config._get_in(before, "text.smart_auto_submit")
    new_val = False if bool(current) else True
    after = config.load(fixture_path)
    _set_nested(after, "text.smart_auto_submit", new_val)
    assert "text.smart_auto_submit" in config.diff_restart_sensitive(before, after)


def test_model_change_is_restart_sensitive(fixture_path: Path) -> None:
    before = config.load(fixture_path)
    after = config.load(fixture_path)
    current = config._get_in(before, "whisper.model")
    new_model = "tiny.en" if str(current) != "tiny.en" else "base.en"
    _set_nested(after, "whisper.model", new_model)
    assert "whisper.model" in config.diff_restart_sensitive(before, after)


def test_hotkey_change_is_restart_sensitive(fixture_path: Path) -> None:
    before = config.load(fixture_path)
    after = config.load(fixture_path)
    _set_nested(after, "hotkey.key", "F13")
    assert "hotkey.key" in config.diff_restart_sensitive(before, after)


# ---------------------------------------------------------------------------
# Edge: uncommenting [text] in an all-commented fixture
# ---------------------------------------------------------------------------

def test_materializing_text_section_from_comments_only() -> None:
    """When the only mention of [text] is in a comment block (e.g.
    all_sections_commented.toml), adding a replacement should create a fresh
    [text] section without touching the commented hints."""
    from .conftest import FIXTURES

    src = FIXTURES / "all_sections_commented.toml"
    content = src.read_text()
    assert "# [text]" in content, "fixture precondition: [text] is commented"
    assert "# replacements = " in content, "fixture precondition: replacements commented"

    doc = tomlkit.parse(content)
    config.set_replacements(doc, {"vox type": "voxtype"})
    dumped = tomlkit.dumps(doc)

    # Commented hints must survive untouched.
    assert "# [text]" in dumped
    assert "# replacements = " in dumped
    # New section materialized.
    assert "\n[text]\n" in dumped or dumped.startswith("[text]\n") or \
           "[text]\nreplacements" in dumped, (
               f"[text] not materialized visibly:\n{dumped[-300:]}"
           )
    assert '"vox type"' in dumped and '"voxtype"' in dumped
