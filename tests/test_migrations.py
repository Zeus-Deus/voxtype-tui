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
