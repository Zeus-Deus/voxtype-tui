"""CLI entry point wired into Voxtype's `[output.post_process] command`.

Contract (per Voxtype's `src/output/post_process.rs`):
  - Transcript arrives on stdin as UTF-8.
  - Processed text goes to stdout; trailing `\n` is stripped by Voxtype.
  - Non-zero exit → Voxtype logs a warning and uses the original text.
  - Timeout (default 30s) → hard kill, original text used.

We read the sidecar (sole source of truth for dictionary rules + their
`to_text` values and categories, and for the vocabulary) and run two
stages, in this order:

  1. the Vexis-style dictionary engine (`dictionary_engine`), then
  2. the vocabulary corrector (`vocab_corrector`), which rewrites misheard
     vocabulary terms ("clawed code" -> "Claude Code"). Always on; it
     only acts when the vocabulary is non-empty.

Each stage fails open on its own, so a broken rule never disables the
corrector and vice versa.

Failure mode: fail-open. Any unexpected exception — missing sidecar,
malformed JSON, regex error — is caught; we write stdin verbatim to
stdout and exit 0. That keeps the user's transcription flowing even if
our layer breaks, at the cost of temporarily losing fuzzy/capitalization
processing until they fix the config. Errors log to stderr so
`journalctl --user -u voxtype` carries a breadcrumb.

Imports are kept minimal (stdlib only) because this process spawns on
every transcription.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

from . import vocab_corrector
from .dictionary_engine import (
    CAPITALIZATION,
    REPLACEMENT,
    DictionaryEngine,
    Rule,
)

SIDECAR_PATH = Path.home() / ".config" / "voxtype-tui" / "metadata.json"
CONFIG_PATH = Path.home() / ".config" / "voxtype" / "config.toml"


def _read_sidecar(sidecar_path: Path = SIDECAR_PATH) -> dict:
    """Parse the sidecar JSON; {} when missing or malformed."""
    if not sidecar_path.exists():
        return {}
    try:
        data = json.loads(sidecar_path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_vocab(sc_data: dict) -> list[str]:
    """Vocabulary phrases from parsed sidecar data (bad entries skipped)."""
    raw = sc_data.get("vocabulary", [])
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        phrase = entry.get("phrase") if isinstance(entry, dict) else entry
        if isinstance(phrase, str) and phrase.strip():
            out.append(phrase.strip())
    return out


def _load_rules(
    sidecar_path: Path = SIDECAR_PATH,
    config_path: Path = CONFIG_PATH,
    sc_data: dict | None = None,
) -> list[Rule]:
    """Build the rule list by joining sidecar (category + to_text) with
    config.toml `[text].replacements` (to_text fallback for entries that
    pre-date the to_text-in-sidecar migration).

    Sidecar is authoritative for category. Replacement-category entries
    without a sidecar `to_text` fall back to config.toml's map. If
    neither has a `to_text`, the rule is skipped.
    """
    if sc_data is None:
        sc_data = _read_sidecar(sidecar_path)

    sc_reps = sc_data.get("replacements", [])
    if not isinstance(sc_reps, list) or not sc_reps:
        return []

    config_reps: dict[str, str] = {}
    if config_path.exists():
        try:
            import tomllib
            with config_path.open("rb") as f:
                doc = tomllib.load(f)
            raw = doc.get("text", {}).get("replacements", {})
            if isinstance(raw, dict):
                config_reps = {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError):
            config_reps = {}

    rules: list[Rule] = []
    for entry in sc_reps:
        if not isinstance(entry, dict):
            continue
        from_text = entry.get("from_text") or entry.get("from") or ""
        if not from_text:
            continue
        category = entry.get("category", REPLACEMENT)
        if category == "Command":  # legacy alias
            category = REPLACEMENT
        if category not in (REPLACEMENT, CAPITALIZATION):
            category = REPLACEMENT
        to_text = entry.get("to_text")
        if not to_text:
            to_text = config_reps.get(from_text)
        if not to_text:
            continue
        rules.append(Rule(trigger=from_text, replacement=to_text, category=category))
    return rules


def _passthrough_note() -> None:
    if os.environ.get("VOXTYPE_TUI_POSTPROCESS_DEBUG"):
        traceback.print_exc(file=sys.stderr)
    else:
        sys.stderr.write("voxtype-tui-postprocess: passthrough on error\n")


def process(
    text: str,
    sidecar_path: Path = SIDECAR_PATH,
    config_path: Path = CONFIG_PATH,
) -> str:
    """Dictionary engine first, then the vocabulary corrector.

    Each stage is fail-open: an exception leaves that stage's input
    unchanged and the next stage still runs."""
    sc_data = _read_sidecar(sidecar_path)
    try:
        rules = _load_rules(sidecar_path, config_path, sc_data=sc_data)
        if rules:
            text = DictionaryEngine(rules).process(text)
    except Exception:  # fail-open — never drop a user's transcript
        _passthrough_note()
    try:
        vocab = _load_vocab(sc_data)
        if vocab:
            text = vocab_corrector.correct(text, vocab)
    except Exception:
        _passthrough_note()
    return text


def main() -> int:
    text = sys.stdin.read()
    try:
        sys.stdout.write(process(text))
        return 0
    except Exception:  # fail-open — never drop a user's transcript
        _passthrough_note()
        sys.stdout.write(text)
        return 0


if __name__ == "__main__":
    sys.exit(main())
