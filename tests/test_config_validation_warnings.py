"""Voxtype may exit zero after replacing unreadable config sections with defaults."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from voxtype_tui import config


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("diagnostic", [
    "Config section 'engine' could not be read and is using defaults. Check it against `voxtype config schema`.",
    "Config section 'audio' could not be read and is using defaults. Check it against `voxtype config schema`.",
    "2 config section(s) were skipped: engine, audio",
])
@pytest.mark.parametrize("color", [False, True])
def test_zero_exit_with_skipped_sections_is_rejected(monkeypatch, tmp_path: Path, stream, diagnostic, color):
    warning = f"2026-01-01T00:00:00Z WARN {diagnostic}\n"
    if color:
        warning = f"\x1b[33m{warning}\x1b[0m"
    result = subprocess.CompletedProcess(["voxtype"], 0, stdout="Current Configuration\n", stderr="")
    setattr(result, stream, getattr(result, stream) + warning)
    monkeypatch.setattr(config.shutil, "which", lambda _: "/fixture/voxtype")
    monkeypatch.setattr(config.subprocess, "run", lambda *a, **k: result)
    ok, message = config.validate_with_voxtype(tmp_path / "config.toml")
    assert not ok
    assert diagnostic in message
    assert "\x1b" not in message
    assert "Current Configuration" not in message


def test_validation_requests_warnings_even_when_parent_suppresses_logging(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RUST_LOG", "error")
    monkeypatch.setenv("VOXTYPE_VALIDATION_CONTROL", "preserved")
    monkeypatch.setattr(config.shutil, "which", lambda _: "/fixture/voxtype")
    captured = {}
    def run(argv, **kwargs):
        captured.update(kwargs)
        assert argv == ["voxtype", "-c", str(tmp_path / "config.toml"), "config"]
        return subprocess.CompletedProcess(argv, 0, stdout="Current Configuration\n", stderr="")
    monkeypatch.setattr(config.subprocess, "run", run)
    assert config.validate_with_voxtype(tmp_path / "config.toml", timeout=2) == (True, "")
    assert captured["env"]["RUST_LOG"] == "warn"
    assert captured["env"]["VOXTYPE_VALIDATION_CONTROL"] == "preserved"
    assert captured["timeout"] == 2
    assert config.os.environ["RUST_LOG"] == "error"


@pytest.mark.parametrize("output", [
    "2026-01-01T00:00:00Z WARN optional audio device probe unavailable\nCurrent Configuration\n",
    "initial_prompt = \"Config section 'engine' could not be read and is using defaults.\"\n",
    "initial_prompt = \"2 config section(s) were skipped: engine, audio\"\n",
])
def test_unrelated_warnings_and_prompt_text_do_not_reject_config(monkeypatch, tmp_path: Path, output):
    monkeypatch.setattr(config.shutil, "which", lambda _: "/fixture/voxtype")
    monkeypatch.setattr(config.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(["voxtype"], 0, stdout=output, stderr=""))
    assert config.validate_with_voxtype(tmp_path / "config.toml") == (True, "")
