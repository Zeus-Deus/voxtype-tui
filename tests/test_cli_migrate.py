"""Tests for the headless ``--apply-migrations`` subcommand."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from voxtype_tui import cli_migrate, config, sidecar, voxtype_cli


def _write_config(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _write_sidecar(path: Path, *, version: int, replacements: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": version,
        "vocabulary": [],
        "replacements": replacements,
    }))


@pytest.fixture
def no_restart(monkeypatch: pytest.MonkeyPatch):
    """Prevent the CLI from actually shelling out to systemctl."""
    calls: list[tuple[str, ...]] = []

    def _fake_restart() -> tuple[bool, str]:
        calls.append(("restart",))
        return True, "ok"

    def _fake_active() -> bool:
        return True

    monkeypatch.setattr(voxtype_cli, "restart_daemon", _fake_restart)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", _fake_active)
    return calls


@pytest.fixture
def skip_voxtype_validation(monkeypatch: pytest.MonkeyPatch):
    """The test machine may not have `voxtype` installed or matching our
    config schema. Bypass pre-save validation."""
    monkeypatch.setattr(config, "validate_with_voxtype", lambda p, timeout=10.0: (True, ""))


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_no_config_file_is_clean_exit(tmp_path: Path) -> None:
    rc = cli_migrate.main([
        "--apply-migrations",
        "--config-path", str(tmp_path / "nope.toml"),
        "--sidecar-path", str(tmp_path / "metadata.json"),
        "--quiet",
    ])
    assert rc == 0


def test_applies_post_process_migration(
    tmp_path: Path, no_restart, skip_voxtype_validation, capsys,
) -> None:
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    _write_config(cfg, '[text]\nreplacements = {"slash foo" = "/foo"}\n')
    _write_sidecar(side, version=1, replacements=[
        {"from_text": "slash foo", "category": "Replacement",
         "added_at": "2026-01-01T00:00:00+00:00"},
    ])
    rc = cli_migrate.main([
        "--apply-migrations",
        "--config-path", str(cfg),
        "--sidecar-path", str(side),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "enable_postprocess" in out
    assert "voxtype daemon restarted" in out
    assert no_restart == [("restart",)]
    # Config file on disk now has our post_process wired up.
    import tomllib
    with cfg.open("rb") as f:
        doc = tomllib.load(f)
    assert doc["output"]["post_process"]["command"] == config.POSTPROCESS_COMMAND


def test_already_migrated_is_noop(
    tmp_path: Path, no_restart, skip_voxtype_validation, capsys,
) -> None:
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    _write_config(cfg, (
        '[text]\nreplacements = {"slash foo" = "/foo"}\n'
        '[output.post_process]\n'
        f'command = "{config.POSTPROCESS_COMMAND}"\n'
        f'timeout_ms = {config.POSTPROCESS_TIMEOUT_MS}\n'
    ))
    _write_sidecar(side, version=sidecar.SCHEMA_VERSION, replacements=[
        {"from_text": "slash foo", "category": "Replacement",
         "added_at": "2026-01-01T00:00:00+00:00"},
    ])
    rc = cli_migrate.main([
        "--apply-migrations",
        "--config-path", str(cfg),
        "--sidecar-path", str(side),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "already current" in out
    assert no_restart == []  # no restart when nothing changed


def test_no_restart_flag_skips_systemctl(
    tmp_path: Path, no_restart, skip_voxtype_validation, capsys,
) -> None:
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    _write_config(cfg, '[text]\nreplacements = {"slash foo" = "/foo"}\n')
    _write_sidecar(side, version=1, replacements=[
        {"from_text": "slash foo", "category": "Replacement",
         "added_at": "2026-01-01T00:00:00+00:00"},
    ])
    rc = cli_migrate.main([
        "--apply-migrations",
        "--config-path", str(cfg),
        "--sidecar-path", str(side),
        "--no-restart",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "skipping daemon restart" in out
    assert no_restart == []


def test_daemon_inactive_skips_restart_cleanly(
    tmp_path: Path, monkeypatch, skip_voxtype_validation, capsys,
) -> None:
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    _write_config(cfg, '[text]\nreplacements = {"slash foo" = "/foo"}\n')
    _write_sidecar(side, version=1, replacements=[
        {"from_text": "slash foo", "category": "Replacement",
         "added_at": "2026-01-01T00:00:00+00:00"},
    ])
    rc = cli_migrate.main([
        "--apply-migrations",
        "--config-path", str(cfg),
        "--sidecar-path", str(side),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "daemon not running" in out


def test_quiet_suppresses_info(
    tmp_path: Path, no_restart, skip_voxtype_validation, capsys,
) -> None:
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    _write_config(cfg, '[text]\nreplacements = {"slash foo" = "/foo"}\n')
    _write_sidecar(side, version=1, replacements=[
        {"from_text": "slash foo", "category": "Replacement",
         "added_at": "2026-01-01T00:00:00+00:00"},
    ])
    rc = cli_migrate.main([
        "--apply-migrations", "--quiet",
        "--config-path", str(cfg),
        "--sidecar-path", str(side),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

def test_restart_failure_exits_nonzero(
    tmp_path: Path, monkeypatch, skip_voxtype_validation, capsys,
) -> None:
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: True)
    monkeypatch.setattr(
        voxtype_cli, "restart_daemon",
        lambda: (False, "Unit voxtype.service not found"),
    )
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    _write_config(cfg, '[text]\nreplacements = {"slash foo" = "/foo"}\n')
    _write_sidecar(side, version=1, replacements=[
        {"from_text": "slash foo", "category": "Replacement",
         "added_at": "2026-01-01T00:00:00+00:00"},
    ])
    rc = cli_migrate.main([
        "--apply-migrations",
        "--config-path", str(cfg),
        "--sidecar-path", str(side),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "failed to restart" in err
