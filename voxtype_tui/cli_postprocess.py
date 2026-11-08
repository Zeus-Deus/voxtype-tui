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
import time
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
        import traceback  # lazy: ~9 ms of startup on every dictation otherwise

        traceback.print_exc(file=sys.stderr)
    else:
        sys.stderr.write("voxtype-tui-postprocess: passthrough on error\n")


def _protected_spans(text: str, rules: list[Rule]) -> list[tuple[int, int]]:
    """Spans of ``text`` that equal a dictionary rule's output. Learned
    aliases must never rewrite inside them (user rules win).
    Over-approximates (any occurrence, fired or not), which only protects
    more text."""
    spans: list[tuple[int, int]] = []
    for r in rules:
        out = r.replacement
        if not out:
            continue
        i = text.find(out)
        while i != -1:
            spans.append((i, i + len(out)))
            i = text.find(out, i + 1)
    spans.sort()
    return spans


def _learned_module():
    """The learned-words stage (auto-learn), when installed (guarded: absent → no-op)."""
    try:
        from . import learned  # type: ignore[attr-defined]
    except Exception:
        return None
    return learned if hasattr(learned, "apply") else None


def process_detailed(
    text: str,
    sidecar_path: Path = SIDECAR_PATH,
    config_path: Path = CONFIG_PATH,
) -> tuple[str, list[str], list[str]]:
    """The full pipeline: dictionary engine, vocabulary corrector, then
    learned words. Returns ``(text, changed_by, learned_hit_ids)``.

    Each stage is fail-open: an exception leaves that stage's input
    unchanged and the next stage still runs."""
    changed_by: list[str] = []
    sc_data = _read_sidecar(sidecar_path)
    rules: list[Rule] = []
    vocab: list[str] = []
    try:
        rules = _load_rules(sidecar_path, config_path, sc_data=sc_data)
        if rules:
            before = text
            text = DictionaryEngine(rules).process(text)
            if text != before:
                changed_by.append("dictionary")
    except Exception:  # fail-open — never drop a user's transcript
        _passthrough_note()
    try:
        vocab = _load_vocab(sc_data)
        if vocab:
            before = text
            text = vocab_corrector.correct(text, vocab)
            if text != before:
                changed_by.append("vocab")
    except Exception:
        _passthrough_note()
    hits: list[str] = []
    learned = _learned_module()
    if learned is not None:
        try:
            spans = _protected_spans(text, rules)
            new_text, hits = learned.apply(
                text, spans, user_phrases=[r.replacement for r in rules] + vocab,
            )
            if isinstance(new_text, str):
                if new_text != text:
                    changed_by.append("learned")
                text = new_text
            hits = [h for h in (hits or []) if isinstance(h, str)]
        except Exception:
            hits = []
            _passthrough_note()
    return text, changed_by, hits


def process(
    text: str,
    sidecar_path: Path = SIDECAR_PATH,
    config_path: Path = CONFIG_PATH,
) -> str:
    """Dictionary engine first, then the vocabulary corrector (then learned
    words, when that module is installed).

    Each stage is fail-open: an exception leaves that stage's input
    unchanged and the next stage still runs."""
    return process_detailed(text, sidecar_path, config_path)[0]


def _capture(raw: str, final: str, changed_by: list[str], hits: list[str]) -> None:
    """Queue one record for usage/history/archive/learning.

    Critical path: one features read, a few stats, one O_APPEND line (and
    one small tmpfs file when the archive or learning needs the text after
    output). No subprocesses, no pruning. Raises on failure; the caller
    has already written the text."""
    from . import capture, features, history

    feats = features.load()
    usage_on = feats["usage"]["enabled"]
    hist_on = feats["history"]["enabled"]
    arch_on = feats["archive"]["enabled"]
    learn_on = feats["learning"]["enabled"]
    if not (usage_on or hist_on or arch_on or learn_on):
        return
    now = time.time()
    rid = history.new_id(now)
    cfg = capture.config_info()
    start_info = capture.read_rec_start()
    if start_info is not None:
        # Consume the recording's start info (never reused). Only the
        # archive needs it after output: hand it to exactly this record
        # (rec-used-<id>.json, claimed by the finalizer); else delete it.
        try:
            if arch_on and start_info.get("wav"):
                os.replace(capture.rec_start_path(), capture.rec_used_path(rid))
            else:
                os.unlink(capture.rec_start_path())
        except OSError:
            pass
    started, duration = capture.measure_duration(start_info, cfg.get("max_duration_secs") or 60, cfg)
    rec = {
        "id": rid,
        "ts": history.iso(now),
        "started_at": history.iso(started) if started is not None else None,
        "duration_s": duration,
        "raw": raw,
        "final": final,
        "words": history.count_words(final),
        "chars": len(final),
        "engine": cfg.get("engine"),
        "model": cfg.get("model"),
        "language": cfg.get("language"),
        "model_uncertain": bool(cfg.get("model_uncertain")),
        "app": (start_info or {}).get("app"),
        "changed_by": changed_by,
        "audio": None,
    }
    if arch_on and feats["archive"]["save_audio"] and start_info and start_info.get("wav") \
            and started is not None:
        rec["audio"] = time.strftime("%Y/%m/%d/", time.localtime(now)) + rec["id"] + ".wav"
    if hits:
        rec["learned_hits"] = hits
    queued = dict(rec, count_usage=bool(usage_on), keep_text=bool(hist_on))
    if not hist_on:
        queued.pop("raw")
        queued.pop("final")
    if usage_on or hist_on:
        history.enqueue(queued)
    if arch_on or learn_on:
        # Runtime copy (RAM) for the post-output hook. Text only when a
        # consumer that is on may keep it: the learning watcher, or the
        # archive with "save text".
        last = rec
        if not (learn_on or (arch_on and feats["archive"]["save_text"])):
            last = {k: v for k, v in rec.items() if k not in ("raw", "final")}
        history.write_handoff(last)


def main() -> int:
    text = sys.stdin.read()
    try:
        final, changed_by, hits = process_detailed(text)
    except Exception:  # fail-open — never drop a user's transcript
        _passthrough_note()
        sys.stdout.write(text)
        return 0
    sys.stdout.write(final)
    sys.stdout.flush()
    try:
        _capture(text, final, changed_by, hits)
    except Exception as e:  # capture must never affect the output
        try:
            from . import history

            history.note_failure("post_process", repr(e))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
