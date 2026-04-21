from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import tomlkit

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"

sys.path.insert(0, str(REPO))

FIXTURE_NAMES = [
    "stock",
    "heavily_customized",
    "externally_edited",
    "minimal",
    "unusual_whitespace",
    "all_sections_commented",
]


@pytest.fixture(params=FIXTURE_NAMES)
def fixture_name(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def fixture_path(fixture_name: str, tmp_path: Path) -> Path:
    """Copy the named fixture to a tmp path so tests can mutate freely."""
    src = FIXTURES / f"{fixture_name}.toml"
    dst = tmp_path / f"{fixture_name}.toml"
    dst.write_text(src.read_text())
    return dst


@pytest.fixture
def voxtype_available() -> bool:
    return shutil.which("voxtype") is not None


def validates_with_voxtype(path: Path) -> tuple[bool, str]:
    """Returns (passes_validation, stderr). Skips gracefully when voxtype
    binary isn't installed (returns True, "")."""
    if shutil.which("voxtype") is None:
        return True, ""
    result = subprocess.run(
        ["voxtype", "-c", str(path), "config"],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0, result.stderr


def count_comment_lines(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.lstrip().startswith("#"))


def comment_lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines() if ln.lstrip().startswith("#")]


def flatten(doc: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a tomlkit doc to {dotted.path: value}. Tables (inline or not)
    recurse; arrays and scalars are values."""
    out: dict[str, Any] = {}
    if not hasattr(doc, "items"):
        return out
    for k, v in doc.items():
        p = f"{prefix}.{k}" if prefix else k
        if hasattr(v, "items") and hasattr(v, "keys"):
            out.update(flatten(v, p))
        else:
            out[p] = _norm(v)
    return out


def _norm(v: Any) -> Any:
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return float(v)
    if isinstance(v, str):
        return str(v)
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def load_toml(path: Path):
    return tomlkit.parse(path.read_text())
