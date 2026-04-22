"""Catalog correctness + install-detection tests.

Three concerns:
  - `MODEL_CATALOG` matches Voxtype's authoritative tables (drift test —
    runs only when the Voxtype source tree is available, else skips).
  - `model_file_path` produces the exact layout Voxtype's validators
    expect for each engine family.
  - `is_model_installed` / `scan_downloaded` correctly detect both
    flat-file (whisper) and directory-based (other 6) engines.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from voxtype_tui import models

# Voxtype source may or may not live on the dev machine. Tests that
# parse it degrade gracefully when absent.
VOXTYPE_SRC = Path.home() / "projects" / "voxtype" / "src"


# ---------------------------------------------------------------------------
# model_file_path — all 7 engines + unknown-engine fallback
# ---------------------------------------------------------------------------

def test_whisper_is_flat_file(tmp_path: Path) -> None:
    assert models.model_file_path("whisper", "tiny.en", models_dir=tmp_path) == (
        tmp_path / "ggml-tiny.en.bin"
    )


def test_parakeet_is_verbatim_dir(tmp_path: Path) -> None:
    assert models.model_file_path("parakeet", "parakeet-tdt-0.6b-v3", models_dir=tmp_path) == (
        tmp_path / "parakeet-tdt-0.6b-v3"
    )


def test_moonshine_has_prefix_in_dir(tmp_path: Path) -> None:
    # Voxtype stores moonshine dirs as `moonshine-<name>` (setup/model.rs
    # validate_moonshine_model). We must match that exactly.
    assert models.model_file_path("moonshine", "base-ja", models_dir=tmp_path) == (
        tmp_path / "moonshine-base-ja"
    )


def test_sensevoice_paraformer_dolphin_omnilingual_prefixed(tmp_path: Path) -> None:
    for engine, name, expected in [
        ("sensevoice", "small", "sensevoice-small"),
        ("paraformer", "zh", "paraformer-zh"),
        ("dolphin", "base", "dolphin-base"),
        ("omnilingual", "300m", "omnilingual-300m"),
    ]:
        assert models.model_file_path(engine, name, models_dir=tmp_path) == (
            tmp_path / expected
        ), f"{engine!r} path mismatch"


def test_unknown_engine_falls_back_gracefully(tmp_path: Path) -> None:
    """Future engines shouldn't crash the TUI — fall through to a best-guess."""
    p = models.model_file_path("some-future-engine", "foo", models_dir=tmp_path)
    assert p == tmp_path / "foo.bin"


# ---------------------------------------------------------------------------
# is_model_installed
# ---------------------------------------------------------------------------

def test_whisper_installed_requires_nonempty_file(tmp_path: Path) -> None:
    p = tmp_path / "ggml-tiny.en.bin"
    p.write_bytes(b"")  # zero-byte file → NOT installed
    assert not models.is_model_installed("whisper", "tiny.en", models_dir=tmp_path)
    p.write_bytes(b"x" * 100)
    assert models.is_model_installed("whisper", "tiny.en", models_dir=tmp_path)


def test_directory_engine_installed_requires_at_least_one_file(tmp_path: Path) -> None:
    d = tmp_path / "moonshine-tiny"
    d.mkdir()
    # Empty dir → NOT installed, matches Voxtype validator behavior.
    assert not models.is_model_installed("moonshine", "tiny", models_dir=tmp_path)
    (d / "encoder_model.onnx").write_bytes(b"x" * 10)
    assert models.is_model_installed("moonshine", "tiny", models_dir=tmp_path)


def test_missing_returns_false(tmp_path: Path) -> None:
    assert not models.is_model_installed("whisper", "nope", models_dir=tmp_path)
    assert not models.is_model_installed("parakeet", "nope", models_dir=tmp_path)


# ---------------------------------------------------------------------------
# scan_downloaded
# ---------------------------------------------------------------------------

def test_scan_whisper_finds_ggml_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path)
    (tmp_path / "ggml-base.en.bin").write_bytes(b"x" * 100)
    (tmp_path / "ggml-tiny.en.bin").write_bytes(b"y" * 50)
    # Non-ggml files must not appear in whisper scan.
    (tmp_path / "unrelated.txt").write_text("hi")
    out = models.scan_downloaded("whisper")
    assert set(out.keys()) == {"base.en", "tiny.en"}
    assert out["tiny.en"] == 50


def test_scan_moonshine_strips_prefix(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path)
    (tmp_path / "moonshine-base-ja").mkdir()
    (tmp_path / "moonshine-base-ja" / "encoder.onnx").write_bytes(b"x" * 100)
    (tmp_path / "ggml-tiny.en.bin").write_bytes(b"y" * 50)  # whisper, not moonshine
    out = models.scan_downloaded("moonshine")
    assert set(out.keys()) == {"base-ja"}


def test_scan_parakeet_uses_verbatim_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path)
    d = tmp_path / "parakeet-tdt-0.6b-v3"
    d.mkdir()
    (d / "encoder-model.onnx").write_bytes(b"x" * 200)
    # Something random that doesn't match the parakeet- prefix must be ignored.
    (tmp_path / "other-engine-thing").mkdir()
    out = models.scan_downloaded("parakeet")
    assert set(out.keys()) == {"parakeet-tdt-0.6b-v3"}


def test_scan_missing_dir_returns_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path / "nope")
    assert models.scan_downloaded("whisper") == {}
    assert models.scan_downloaded("moonshine") == {}


# ---------------------------------------------------------------------------
# Catalog completeness
# ---------------------------------------------------------------------------

def test_all_seven_engines_present_and_non_empty() -> None:
    """Every engine Voxtype supports must offer at least one model — no
    more empty placeholder lists."""
    expected = {"whisper", "parakeet", "moonshine", "sensevoice",
                "paraformer", "dolphin", "omnilingual"}
    assert set(models.MODEL_CATALOG.keys()) == expected
    for engine, rows in models.MODEL_CATALOG.items():
        assert rows, f"{engine!r} catalog is empty — at least one model required"


# ---------------------------------------------------------------------------
# Drift detection — only runs when Voxtype source is checked out
# ---------------------------------------------------------------------------

pytestmark_drift = pytest.mark.skipif(
    not (VOXTYPE_SRC / "setup" / "model.rs").exists(),
    reason="Voxtype source not at ~/projects/voxtype — drift check skipped",
)


@pytestmark_drift
def test_whisper_catalog_matches_voxtype_source() -> None:
    """All whisper model NAMES we advertise must appear in Voxtype's
    MODELS table. Sizes are best-effort and not compared (Voxtype's
    and ours round differently)."""
    src = (VOXTYPE_SRC / "setup" / "model.rs").read_text()
    # Extract every `name: "<name>"` string inside WhisperModelInfo-style entries.
    names = set(re.findall(r'name:\s*"([\w.\-]+)"', src))
    our_whisper = {m.name for m in models.MODEL_CATALOG["whisper"]}
    missing_ours = our_whisper - names
    assert not missing_ours, (
        f"Our whisper catalog has models Voxtype doesn't know about: {missing_ours}. "
        f"Either Voxtype removed them or our list drifted. Review "
        f"{VOXTYPE_SRC}/setup/model.rs."
    )


@pytestmark_drift
def test_moonshine_catalog_matches_voxtype_source() -> None:
    """Moonshine-specific — Voxtype's names vs ours."""
    src = (VOXTYPE_SRC / "setup" / "model.rs").read_text()
    # Moonshine rows have dir_name = "moonshine-<x>" and name = "<x>".
    # Pick up everything in the moonshine block by looking for dir_name.
    dir_names = set(re.findall(r'dir_name:\s*"moonshine-([\w.\-]+)"', src))
    our_moonshine = {m.name for m in models.MODEL_CATALOG["moonshine"]}
    assert our_moonshine <= dir_names, (
        f"Our moonshine catalog has names Voxtype doesn't ship: "
        f"{our_moonshine - dir_names}"
    )
