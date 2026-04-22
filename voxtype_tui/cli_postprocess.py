"""CLI entry point wired into Voxtype's `[output.post_process] command`.

Contract (per Voxtype's `src/output/post_process.rs`):
  - Transcript arrives on stdin as UTF-8.
  - Processed text goes to stdout; trailing `\n` is stripped by Voxtype.
  - Non-zero exit → Voxtype logs a warning and uses the original text.
  - Timeout (default 30s) → hard kill, original text used.

We read the sidecar (sole source of truth for dictionary rules + their
`to_text` values and categories) and apply the Vexis-style engine.

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

from .dictionary_engine import (
    CAPITALIZATION,
    REPLACEMENT,
    DictionaryEngine,
    Rule,
)

SIDECAR_PATH = Path.home() / ".config" / "voxtype-tui" / "metadata.json"
CONFIG_PATH = Path.home() / ".config" / "voxtype" / "config.toml"


def _load_rules(
    sidecar_path: Path = SIDECAR_PATH,
    config_path: Path = CONFIG_PATH,
) -> list[Rule]:
    """Build the rule list by joining sidecar (category + to_text) with
    config.toml `[text].replacements` (to_text fallback for entries that
    pre-date the to_text-in-sidecar migration).

    Sidecar is authoritative for category. Replacement-category entries
    without a sidecar `to_text` fall back to config.toml's map. If
    neither has a `to_text`, the rule is skipped.
    """
    sc_data: dict = {}
    if sidecar_path.exists():
        try:
            sc_data = json.loads(sidecar_path.read_text())
        except (OSError, json.JSONDecodeError):
            sc_data = {}

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
    sc_reps = sc_data.get("replacements", [])
    if not isinstance(sc_reps, list):
        return rules

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


def main() -> int:
    text = sys.stdin.read()
    try:
        rules = _load_rules()
        if not rules:
            sys.stdout.write(text)
            return 0
        engine = DictionaryEngine(rules)
        sys.stdout.write(engine.process(text))
        return 0
    except Exception:  # fail-open — never drop a user's transcript
        if os.environ.get("VOXTYPE_TUI_POSTPROCESS_DEBUG"):
            traceback.print_exc(file=sys.stderr)
        else:
            sys.stderr.write("voxtype-tui-postprocess: passthrough on error\n")
        sys.stdout.write(text)
        return 0


if __name__ == "__main__":
    sys.exit(main())
