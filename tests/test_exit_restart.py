"""Tests for the exit-time auto-restart hook (_restart_daemon_on_exit_if_needed).

The hook is a plain function called in `main()` after `app.run()` returns.
These tests build an AppState stand-in and verify each branch of the hook,
plus the broadened daemon_stale flag on AppState.save().
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from voxtype_tui import app as app_module
from voxtype_tui import config, sidecar, voxtype_cli
from voxtype_tui.state import AppState


# ---------------------------------------------------------------------------
# Hook branches
# ---------------------------------------------------------------------------

def test_hook_noop_when_state_is_none() -> None:
    """Pre-load failure path: app.state stayed None, nothing to do."""
    fake = SimpleNamespace(state=None)
    # No crash, no side effects — just a silent return.
    app_module._restart_daemon_on_exit_if_needed(fake)  # type: ignore[arg-type]


def test_hook_noop_when_not_stale(monkeypatch, capsys) -> None:
    """Clean session (daemon up-to-date): no restart, no output."""
    calls: list[str] = []
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: calls.append("active") or True)
    monkeypatch.setattr(voxtype_cli, "restart_daemon", lambda: (_ for _ in ()).throw(AssertionError("should not be called")))

    fake = SimpleNamespace(state=SimpleNamespace(daemon_stale=False))
    app_module._restart_daemon_on_exit_if_needed(fake)  # type: ignore[arg-type]

    assert calls == []  # is_daemon_active shouldn't even be probed
    assert capsys.readouterr().out == ""


def test_hook_noop_when_daemon_inactive(monkeypatch, capsys) -> None:
    """Stale state but daemon isn't running → nothing to restart."""
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "restart_daemon", lambda: (_ for _ in ()).throw(AssertionError("should not be called")))

    fake = SimpleNamespace(state=SimpleNamespace(daemon_stale=True))
    app_module._restart_daemon_on_exit_if_needed(fake)  # type: ignore[arg-type]

    assert capsys.readouterr().out == ""


def test_hook_restarts_when_stale_and_active(monkeypatch, capsys) -> None:
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: True)
    monkeypatch.setattr(voxtype_cli, "restart_daemon", lambda: (True, "ok"))

    fake = SimpleNamespace(state=SimpleNamespace(daemon_stale=True))
    app_module._restart_daemon_on_exit_if_needed(fake)  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "restarting voxtype daemon" in out
    assert "voxtype daemon restarted" in out


def test_hook_reports_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: True)
    monkeypatch.setattr(voxtype_cli, "restart_daemon", lambda: (False, "systemd exploded"))

    fake = SimpleNamespace(state=SimpleNamespace(daemon_stale=True))
    app_module._restart_daemon_on_exit_if_needed(fake)  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "daemon restart failed" in out
    assert "systemd exploded" in out
    assert "systemctl --user restart voxtype" in out


# ---------------------------------------------------------------------------
# Broadened daemon_stale on save
# ---------------------------------------------------------------------------

@pytest.fixture
def app_state(tmp_path: Path, monkeypatch) -> AppState:
    """Build an AppState with validation disabled so save() works without
    a real `voxtype` binary on PATH."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[text]\nspoken_punctuation = true\n")
    side = tmp_path / "metadata.json"
    side.write_text(json.dumps({
        "version": sidecar.SCHEMA_VERSION,
        "vocabulary": [], "replacements": [],
    }))
    monkeypatch.setattr(config, "validate_with_voxtype", lambda p, timeout=10.0: (True, ""))
    return AppState.load(cfg, side)


def test_save_sets_stale_on_any_config_change(app_state) -> None:
    """Even non-restart-sensitive config writes flip daemon_stale — the
    daemon reads config once at startup, so every write needs a reload."""
    # spoken_punctuation is not in RESTART_SENSITIVE_PATHS... wait, it is.
    # Use something that's definitely not on the narrow list.
    app_state.set_setting("output.type_delay_ms", 25)
    assert app_state.config_dirty is True
    app_state.save()
    assert app_state.daemon_stale is True


def test_save_does_not_set_stale_for_sidecar_only_edit(app_state) -> None:
    """Sidecar-only mutations (category flips, notes) do not restart the
    daemon — our post-process CLI re-reads metadata.json on every run."""
    # Add a replacement, save (that's a config change; first save can set stale).
    app_state.upsert_replacement("foo", "bar", "Replacement")
    app_state.save()
    # Clear stale as if user restarted via pill.
    app_state.daemon_stale = False
    # Now do a sidecar-only change: flip its category.
    app_state.cycle_replacement_category("foo")
    assert app_state.sidecar_dirty is True
    assert app_state.config_dirty is False
    app_state.save()
    # Config was not touched → daemon_stale stays False.
    assert app_state.daemon_stale is False


def test_save_with_no_changes_does_not_set_stale(app_state) -> None:
    """Empty save (nothing dirty) must not flip daemon_stale."""
    app_state.save()
    assert app_state.daemon_stale is False
