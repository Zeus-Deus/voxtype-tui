"""UI-only metadata that doesn't belong in Voxtype's config.

Stores per-vocabulary-phrase and per-replacement metadata (category, notes,
added_at). Voxtype's config.toml is the source of truth for *which* entries
exist; the sidecar decorates them.

Reconciliation rules:
  - Vocabulary: canonical form is ", "-joined phrases. If config.toml matches
    what the sidecar would produce, keep the sidecar. If they diverge (user
    hand-edited config.toml), rebuild from config and warn.
  - Replacements: config is source of truth for keys; sidecar provides category
    metadata. Orphans dropped; keys with no metadata default to "Replacement".
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SIDECAR_PATH = Path.home() / ".config" / "voxtype-tui" / "metadata.json"

# Current sidecar schema. Bumped in lockstep with any new entry in
# `migrations.MIGRATIONS`. Files on disk tagged with a lower version
# trigger pending migrations on load; fresh (in-memory) sidecars start
# at this version so there's nothing to migrate for a brand-new install.
# The "missing version field" fallback stays at 1 to catch pre-migration
# sidecars written before this field was added.
SCHEMA_VERSION = 2

CATEGORIES: tuple[str, ...] = ("Replacement", "Capitalization")
DEFAULT_CATEGORY = "Replacement"

# Sidecar files created by earlier versions may carry entries tagged
# "Command". Vexis collapsed this category into "Replacement" (the two are
# functionally identical — both are flat literal rewrites on disk) and
# voxtype-tui follows the same simplification. Load-time normalization
# transparently rewrites "Command" → "Replacement"; the disk file picks up
# the new spelling on the next save.
_LEGACY_CATEGORY_MIGRATIONS: dict[str, str] = {"Command": "Replacement"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class VocabEntry:
    phrase: str
    added_at: str = field(default_factory=_now_iso)
    notes: str | None = None


@dataclass
class ReplacementEntry:
    from_text: str
    category: str = DEFAULT_CATEGORY
    added_at: str = field(default_factory=_now_iso)


@dataclass
class Sidecar:
    vocabulary: list[VocabEntry] = field(default_factory=list)
    replacements: list[ReplacementEntry] = field(default_factory=list)
    version: int = SCHEMA_VERSION


def load(path: Path = SIDECAR_PATH) -> Sidecar:
    if not path.exists():
        # Brand-new install — no on-disk history to migrate. Start at
        # the current schema so migration runs short-circuit.
        return Sidecar(version=SCHEMA_VERSION)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return Sidecar(version=SCHEMA_VERSION)
    vocab = [VocabEntry(**v) for v in data.get("vocabulary", [])]
    reps: list[ReplacementEntry] = []
    for r in data.get("replacements", []):
        cat = r.get("category", DEFAULT_CATEGORY)
        # Silent migration: older sidecar files may tag entries "Command".
        # We fold that into "Replacement" — no warning, no user prompt.
        cat = _LEGACY_CATEGORY_MIGRATIONS.get(cat, cat)
        if cat not in CATEGORIES:
            cat = DEFAULT_CATEGORY
        reps.append(ReplacementEntry(
            from_text=r.get("from_text") or r.get("from", ""),
            category=cat,
            added_at=r.get("added_at", _now_iso()),
        ))
    return Sidecar(
        vocabulary=vocab,
        replacements=reps,
        version=data.get("version", 1),
    )


def save_atomic(sc: Sidecar, path: Path = SIDECAR_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": sc.version,
        "vocabulary": [asdict(v) for v in sc.vocabulary],
        "replacements": [asdict(r) for r in sc.replacements],
    }
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        if Path(tmp).exists():
            os.unlink(tmp)
        raise


def build_initial_prompt(vocab: Iterable[VocabEntry]) -> str:
    return ", ".join(v.phrase for v in vocab)


def parse_initial_prompt(s: str | None) -> list[str]:
    if not s:
        return []
    return [p.strip() for p in s.split(",") if p.strip()]


def reconcile_vocab(
    sc_vocab: list[VocabEntry],
    initial_prompt: str | None,
) -> tuple[list[VocabEntry], list[str]]:
    expected = build_initial_prompt(sc_vocab)
    on_disk = initial_prompt or ""
    if on_disk == expected:
        return sc_vocab, []
    # Divergence. Rebuild from disk, preserving metadata for matched phrases.
    disk_phrases = parse_initial_prompt(initial_prompt)
    by_phrase = {v.phrase: v for v in sc_vocab}
    new_vocab = [
        by_phrase[p] if p in by_phrase else VocabEntry(phrase=p)
        for p in disk_phrases
    ]
    if not sc_vocab and not initial_prompt:
        return new_vocab, []
    return new_vocab, [
        f"vocabulary diverged — reloaded {len(new_vocab)} entries from config.toml"
    ]


def reconcile_replacements(
    sc_reps: list[ReplacementEntry],
    config_reps: dict[str, str],
) -> tuple[list[ReplacementEntry], list[str]]:
    by_from = {r.from_text: r for r in sc_reps}
    new_reps: list[ReplacementEntry] = []
    added = 0
    for from_text in config_reps:
        if from_text in by_from:
            entry = by_from[from_text]
            # Defense-in-depth migration: a sidecar entry with a category
            # that's no longer valid (e.g. legacy "Command" that slipped
            # past load-time normalization) gets quietly reset to the
            # default. NOT flagged as a divergence — it's a schema-level
            # migration, not a user-visible external edit.
            if entry.category not in CATEGORIES:
                entry = ReplacementEntry(
                    from_text=entry.from_text,
                    category=DEFAULT_CATEGORY,
                    added_at=entry.added_at,
                )
            new_reps.append(entry)
        else:
            new_reps.append(ReplacementEntry(from_text=from_text))
            added += 1
    dropped = len(sc_reps) - (len(new_reps) - added)
    warnings: list[str] = []
    if added:
        warnings.append(
            f"{added} replacement(s) in config.toml had no sidecar metadata "
            f"— defaulted to '{DEFAULT_CATEGORY}'"
        )
    if dropped > 0:
        warnings.append(
            f"dropped {dropped} orphaned sidecar replacement entries not in config.toml"
        )
    return new_reps, warnings
