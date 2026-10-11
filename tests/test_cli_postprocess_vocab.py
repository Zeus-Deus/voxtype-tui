"""`voxtype-tui-postprocess` with vocabulary: dictionary engine first, then
the always-on vocabulary corrector.

Runs the CLI as `python -m voxtype_tui.cli_postprocess` from this checkout
under a temp HOME, so it doesn't depend on the console script being
installed. Fixtures are generic.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from voxtype_tui import cli_postprocess, vocab_corrector

REPO = Path(__file__).resolve().parent.parent
VOCAB = ["Claude Code", "Codemux", "Hyprland", "passpage"]


def _write_sidecar(home: Path, vocabulary: list, replacements: list | None = None) -> Path:
    p = home / ".config" / "voxtype-tui" / "metadata.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "version": 3,
        "vocabulary": [{"phrase": v, "added_at": "2026-01-01T00:00:00+00:00"}
                       if isinstance(v, str) else v for v in vocabulary],
        "replacements": replacements or [],
    }))
    return p


def _run(home: Path, text: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO)}
    return subprocess.run(
        [sys.executable, "-m", "voxtype_tui.cli_postprocess"],
        input=text, capture_output=True, text=True, env=env, timeout=20,
    )


def test_cli_corrects_vocabulary_without_dictionary_rules(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, VOCAB)
    proc = _run(tmp_path, "open clawed code and check code mux, then the pass page")
    assert proc.returncode == 0
    assert proc.stdout == "open Claude Code and check Codemux, then the passpage"


def test_cli_runs_dictionary_before_corrector(tmp_path: Path) -> None:
    # The dictionary rule turns "slash deploy now" into text that contains a
    # misheard vocab term; the corrector must see the rule's output.
    _write_sidecar(tmp_path, VOCAB, [{
        "from_text": "slash deploy now", "category": "Replacement",
        "added_at": "2026-01-01T00:00:00+00:00", "to_text": "/deploy code mux",
    }])
    proc = _run(tmp_path, "please slash deploy now")
    assert proc.returncode == 0
    assert proc.stdout == "please /deploy Codemux"


def test_cli_passthrough_without_vocab_or_rules(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, [])
    proc = _run(tmp_path, "code mux stays\n")
    assert proc.returncode == 0
    assert proc.stdout == "code mux stays\n"


# ---------------------------------------------------------------------------
# In-process: process() fail-open per stage
# ---------------------------------------------------------------------------

def test_process_corrector_survives_broken_dictionary(tmp_path: Path, monkeypatch) -> None:
    sc = _write_sidecar(tmp_path, VOCAB, [{
        "from_text": "x", "category": "Replacement", "to_text": "y",
        "added_at": "2026-01-01T00:00:00+00:00",
    }])

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("bad rule")

    monkeypatch.setattr(cli_postprocess, "DictionaryEngine", Boom)
    out = cli_postprocess.process("check code mux", sc, tmp_path / "none.toml")
    assert out == "check Codemux"


def test_process_dictionary_survives_broken_corrector(tmp_path: Path, monkeypatch) -> None:
    sc = _write_sidecar(tmp_path, VOCAB, [{
        "from_text": "foo bar", "category": "Replacement", "to_text": "baz",
        "added_at": "2026-01-01T00:00:00+00:00",
    }])
    monkeypatch.setattr(vocab_corrector, "_fix", lambda *a: 1 / 0)
    out = cli_postprocess.process("say foo bar code mux", sc, tmp_path / "none.toml")
    assert out == "say baz code mux"


@pytest.mark.parametrize("payload", [
    "{ not json",
    "[1, 2, 3]",
    json.dumps({"vocabulary": "Codemux"}),
    json.dumps({"vocabulary": [None, 3, {"phrase": 7}, {"nope": 1}]}),
])
def test_process_malformed_sidecar_passes_through(tmp_path: Path, payload: str) -> None:
    sc = tmp_path / "metadata.json"
    sc.write_text(payload)
    assert cli_postprocess.process("check code mux", sc, tmp_path / "none.toml") == "check code mux"


def test_load_vocab_accepts_plain_strings_and_entries() -> None:
    data = {"vocabulary": ["Codemux", {"phrase": " Hyprland "}, {"phrase": ""}, None]}
    assert cli_postprocess._load_vocab(data) == ["Codemux", "Hyprland"]
