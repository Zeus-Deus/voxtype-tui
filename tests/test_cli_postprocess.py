"""End-to-end tests for `voxtype-tui-postprocess`.

Spawns the real installed console script so stdin/stdout protocol and
fail-open behavior match what the Voxtype daemon will see.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = shutil.which("voxtype-tui-postprocess")
pytestmark = pytest.mark.skipif(
    BIN is None, reason="voxtype-tui-postprocess not installed (run pip install -e .)"
)


def _run(input_text: str, *, sidecar: Path, config: Path) -> tuple[str, str, int]:
    env = {"HOME": str(sidecar.parent.parent.parent)}
    proc = subprocess.run(
        [BIN],
        input=input_text,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "HOME": env["HOME"]},
        timeout=10,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _write_sidecar(root: Path, replacements: list[dict]) -> Path:
    p = root / ".config" / "voxtype-tui" / "metadata.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": 1, "vocabulary": [], "replacements": replacements}))
    return p


def _write_config(root: Path, replacements: dict[str, str]) -> Path:
    p = root / ".config" / "voxtype" / "config.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "[text]\nreplacements = {"
    body += ", ".join(f'"{k}" = "{v}"' for k, v in replacements.items())
    body += "}\n"
    p.write_text(body)
    return p


# ---------------------------------------------------------------------------

def test_passthrough_when_no_sidecar(tmp_path: Path) -> None:
    sc = tmp_path / ".config" / "voxtype-tui" / "metadata.json"
    sc.parent.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / ".config" / "voxtype" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    # Neither file exists — CLI must passthrough stdin.
    out, _, rc = _run("hello world\n", sidecar=sc, config=cfg)
    assert rc == 0
    assert out == "hello world\n"


def test_applies_replacement_from_user_config(tmp_path: Path) -> None:
    sc = _write_sidecar(tmp_path, [
        {"from_text": "slash codemux release", "category": "Replacement", "added_at": "2026-01-01T00:00:00+00:00"},
    ])
    _write_config(tmp_path, {"slash codemux release": "/codemux-release"})
    out, _, rc = _run("run slash codemux release now", sidecar=sc, config=tmp_path / ".config" / "voxtype" / "config.toml")
    assert rc == 0
    assert out == "run /codemux-release now"


def test_applies_capitalization_after_replacement(tmp_path: Path) -> None:
    sc = _write_sidecar(tmp_path, [
        {"from_text": "cloud code", "category": "Replacement", "added_at": "2026-01-01T00:00:00+00:00", "to_text": "Claude Code"},
        {"from_text": "typescript", "category": "Capitalization", "added_at": "2026-01-01T00:00:00+00:00", "to_text": "TypeScript"},
    ])
    _write_config(tmp_path, {"cloud code": "Claude Code"})
    out, _, rc = _run("i write typescript in cloud code", sidecar=sc, config=tmp_path / ".config" / "voxtype" / "config.toml")
    assert rc == 0
    assert out == "i write TypeScript in Claude Code"


def test_whisper_variants_collapse_to_canonical(tmp_path: Path) -> None:
    sc = _write_sidecar(tmp_path, [
        {"from_text": "slash codemux release", "category": "Replacement", "added_at": "2026-01-01T00:00:00+00:00"},
    ])
    cfg = _write_config(tmp_path, {"slash codemux release": "/codemux-release"})
    for src in ("run /codemuxrelease now", "run /codemux release now", "run /codemux-release now"):
        out, _, rc = _run(src, sidecar=sc, config=cfg)
        assert rc == 0
        assert out == "run /codemux-release now", f"failed on {src!r} -> {out!r}"


def test_fail_open_on_malformed_sidecar(tmp_path: Path) -> None:
    sc = tmp_path / ".config" / "voxtype-tui" / "metadata.json"
    sc.parent.mkdir(parents=True, exist_ok=True)
    sc.write_text("{ not valid json")
    cfg = tmp_path / ".config" / "voxtype" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    out, _, rc = _run("unchanged\n", sidecar=sc, config=cfg)
    assert rc == 0
    assert out == "unchanged\n"
