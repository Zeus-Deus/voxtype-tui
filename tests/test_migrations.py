"""Unit tests for the migrations module."""
from __future__ import annotations

import tomlkit

from voxtype_tui import config, migrations, sidecar


def _fresh_doc() -> tomlkit.TOMLDocument:
    return tomlkit.parse("")


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------

def test_empty_state_runs_no_migrations() -> None:
    """Fresh install with no rules: nothing to migrate, version advances."""
    doc = _fresh_doc()
    sc = sidecar.Sidecar(version=1)
    result = migrations.run_pending(doc, sc)
    assert result.applied == []
    assert result.touches_config is False
    assert sc.version == migrations.SCHEMA_VERSION


def test_already_at_current_version_is_noop() -> None:
    """Second run does nothing."""
    doc = _fresh_doc()
    sc = sidecar.Sidecar(version=migrations.SCHEMA_VERSION, replacements=[
        sidecar.ReplacementEntry(from_text="x"),
    ])
    result = migrations.run_pending(doc, sc)
    assert result.applied == []
    assert result.touches_config is False


def test_run_is_idempotent() -> None:
    """Run, run again — no additional changes."""
    doc = _fresh_doc()
    sc = sidecar.Sidecar(version=1, replacements=[
        sidecar.ReplacementEntry(from_text="x"),
    ])
    first = migrations.run_pending(doc, sc)
    second = migrations.run_pending(doc, sc)
    # v2 wires the hook; v3 then finds it already wired → no change
    assert first.applied == ["enable_postprocess"]
    assert second.applied == []


# ---------------------------------------------------------------------------
# First migration: enable_postprocess
# ---------------------------------------------------------------------------

def test_enable_postprocess_fires_when_rules_exist() -> None:
    doc = _fresh_doc()
    sc = sidecar.Sidecar(version=1, replacements=[
        sidecar.ReplacementEntry(from_text="slash foo"),
    ])
    result = migrations.run_pending(doc, sc)
    assert "enable_postprocess" in result.applied
    assert result.touches_config is True
    pp = config.get_post_process(doc)
    assert pp.get("command") == config.POSTPROCESS_COMMAND
    assert pp.get("timeout_ms") == config.POSTPROCESS_TIMEOUT_MS


def test_enable_postprocess_skipped_when_no_rules() -> None:
    doc = _fresh_doc()
    sc = sidecar.Sidecar(version=1, replacements=[])
    result = migrations.run_pending(doc, sc)
    assert result.applied == []
    assert config.get_post_process(doc) == {}


def test_enable_postprocess_respects_user_custom_command() -> None:
    """If the user has their own post_process command set, we don't clobber it."""
    doc = _fresh_doc()
    config.set_post_process(doc, "ollama-cleanup.sh", 30000)
    sc = sidecar.Sidecar(version=1, replacements=[
        sidecar.ReplacementEntry(from_text="slash foo"),
    ])
    result = migrations.run_pending(doc, sc)
    assert result.applied == []
    assert config.get_post_process(doc)["command"] == "ollama-cleanup.sh"


def test_enable_postprocess_updates_stale_timeout() -> None:
    """Our command already set but with a non-default timeout gets normalized."""
    doc = _fresh_doc()
    config.set_post_process(doc, config.POSTPROCESS_COMMAND, 99999)
    sc = sidecar.Sidecar(version=1, replacements=[
        sidecar.ReplacementEntry(from_text="slash foo"),
    ])
    result = migrations.run_pending(doc, sc)
    assert "enable_postprocess" in result.applied
    assert config.get_post_process(doc)["timeout_ms"] == config.POSTPROCESS_TIMEOUT_MS


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_broken_migration_does_not_brick_run(monkeypatch) -> None:
    """A migration that raises is logged and skipped; later migrations still run."""
    boom = migrations.Migration(
        target_version=99,
        name="boom",
        description="deliberately broken",
        apply=lambda doc, sc: (_ for _ in ()).throw(RuntimeError("oops")),
        touches_config=True,
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", [boom])
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", 99)
    doc = _fresh_doc()
    sc = sidecar.Sidecar(version=1)
    result = migrations.run_pending(doc, sc)
    # broken migration produced no visible change, but version still advanced
    assert result.applied == []
    assert sc.version == 99


def test_version_advances_even_when_migration_noops() -> None:
    """A migration that returns False still counts toward version advance."""
    doc = _fresh_doc()
    sc = sidecar.Sidecar(version=1)  # no rules → enable_postprocess returns False
    result = migrations.run_pending(doc, sc)
    assert sc.version == migrations.SCHEMA_VERSION
    assert result.applied == []


# ---------------------------------------------------------------------------
# v3: enable_postprocess_for_vocabulary
# ---------------------------------------------------------------------------

def test_schema_version_is_3() -> None:
    assert migrations.SCHEMA_VERSION == sidecar.SCHEMA_VERSION == 3
    assert [m.target_version for m in migrations.MIGRATIONS] == [2, 3]


def test_v3_wires_postprocess_for_vocabulary_only_install() -> None:
    """A v2 install with vocabulary but no dictionary rules was never wired."""
    doc = _fresh_doc()
    sc = sidecar.Sidecar(version=2, vocabulary=[sidecar.VocabEntry(phrase="Codemux")])
    result = migrations.run_pending(doc, sc)
    assert result.applied == ["enable_postprocess_for_vocabulary"]
    assert result.touches_config is True
    pp = config.get_post_process(doc)
    assert pp.get("command") == config.POSTPROCESS_COMMAND
    assert pp.get("timeout_ms") == config.POSTPROCESS_TIMEOUT_MS
    assert sc.version == 3


def test_v3_from_v1_with_vocabulary_only() -> None:
    doc = _fresh_doc()
    sc = sidecar.Sidecar(version=1, vocabulary=[sidecar.VocabEntry(phrase="Codemux")])
    result = migrations.run_pending(doc, sc)
    assert result.applied == ["enable_postprocess_for_vocabulary"]
    assert config.get_post_process(doc).get("command") == config.POSTPROCESS_COMMAND


def test_v3_noop_when_already_wired_by_v2() -> None:
    doc = _fresh_doc()
    config.set_post_process(doc, config.POSTPROCESS_COMMAND, config.POSTPROCESS_TIMEOUT_MS)
    sc = sidecar.Sidecar(
        version=2,
        vocabulary=[sidecar.VocabEntry(phrase="Codemux")],
        replacements=[sidecar.ReplacementEntry(from_text="slash foo")],
    )
    result = migrations.run_pending(doc, sc)
    assert result.applied == []
    assert result.touches_config is False


def test_v3_noop_without_vocabulary_or_rules() -> None:
    doc = _fresh_doc()
    sc = sidecar.Sidecar(version=2)
    result = migrations.run_pending(doc, sc)
    assert result.applied == []
    assert config.get_post_process(doc) == {}
    assert sc.version == 3


def test_v3_respects_user_custom_command() -> None:
    doc = _fresh_doc()
    config.set_post_process(doc, "ollama-cleanup.sh", 30000)
    sc = sidecar.Sidecar(version=2, vocabulary=[sidecar.VocabEntry(phrase="Codemux")])
    result = migrations.run_pending(doc, sc)
    assert result.applied == []
    assert config.get_post_process(doc)["command"] == "ollama-cleanup.sh"


def test_v3_is_idempotent_when_version_rewound() -> None:
    doc = _fresh_doc()
    sc = sidecar.Sidecar(version=2, vocabulary=[sidecar.VocabEntry(phrase="Codemux")])
    migrations.run_pending(doc, sc)
    before = doc.as_string()
    sc.version = 2
    again = migrations.run_pending(doc, sc)
    assert again.applied == []
    assert doc.as_string() == before


def test_wants_post_process_gate() -> None:
    assert not migrations.wants_post_process(sidecar.Sidecar())
    assert migrations.wants_post_process(
        sidecar.Sidecar(vocabulary=[sidecar.VocabEntry(phrase="Codemux")]))
    assert migrations.wants_post_process(
        sidecar.Sidecar(replacements=[sidecar.ReplacementEntry(from_text="x")]))
