"""Portable bundle format (v1.1 sync / export / import).

One file, three purposes — a user may pick any subset:

  1. Syncthing-driven auto-sync between devices (~/.config/voxtype-tui/sync.json)
  2. Manual backup to external storage (export to ~/Downloads/...)
  3. Device-to-device migration (export on A, import on B)

Security model
--------------
The bundle has three distinct blocks:

  * `sync`     — vocabulary, replacements, and portable settings. Safe to
                 share, safe to sync. Never contains secrets.
  * `local`    — per-device settings (hotkey, audio device, VAD, GPU).
                 Included for export-all convenience; the startup reader
                 NEVER applies `local` from sync.json. Imported only when
                 the user explicitly says "import local settings too".
  * `secrets`  — API keys and shell-command fields that are either
                 credentials or RCE/exfil surface. Present ONLY when a
                 manual export ran with `include_secrets=True`. Absent
                 in every auto-sync write.

The absence of the `secrets` block is the one-glance signal that the
file is safe to move around. On import, any dangerous-field change
(API key, endpoint, any `*_command`) is surfaced to the user for
explicit confirmation — the adapter never silently swaps a command
string.

Staleness compare
-----------------
We do not trust wall-clock timestamps across devices. Every bundle
carries `local_sync_hash` — a canonical sha256 of the `sync` block as
it was when we wrote this file. On startup the reader recomputes the
sync hash from live config, compares to the stored `local_sync_hash`,
and uses the comparison to decide whether local has drifted since we
last wrote sync.json:

  * current hash == stored hash → local unchanged → apply the synced
    bundle (it may have been updated by another device).
  * current hash != stored hash → local changed since last sync
    write → local wins, rewrite sync.json.

`generated_at` exists only for display ("Synced from zeus-laptop at …").

This module is pure functions; no filesystem I/O. The writer, reader,
and UI wiring live in later steps.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import tomlkit

from .sidecar import (
    CATEGORIES,
    DEFAULT_CATEGORY,
    ReplacementEntry,
    Sidecar,
    VocabEntry,
    _LEGACY_CATEGORY_MIGRATIONS,
)

logger = logging.getLogger(__name__)

SYNC_PATH = Path.home() / ".config" / "voxtype-tui" / "sync.json"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 1
FORMAT_TAG: str = "voxtype-tui-bundle"

# Hard caps enforced by the parser to prevent OOM and pathological UI.
# 1 MB is three orders of magnitude above any realistic bundle, while still
# protecting against "user pointed me at a 400 MB JSON blob" mistakes.
MAX_BUNDLE_BYTES: int = 1_000_000
MAX_VOCAB_COUNT: int = 500
MAX_VOCAB_PHRASE_LEN: int = 200
MAX_REPLACEMENT_COUNT: int = 1_000
MAX_REPLACEMENT_LEN: int = 500
MAX_SETTING_STRING_LEN: int = 2_048
MAX_DEVICE_LABEL_LEN: int = 128

# Whisper's initial_prompt tokenizes at roughly 4 chars per token. We use the
# cheap heuristic here rather than pulling in tiktoken — the only consumer is
# a warning banner, and false positives on "close to limit" are harmless.
WHISPER_INITIAL_PROMPT_TOKEN_LIMIT: int = 224
APPROX_CHARS_PER_TOKEN: int = 4

# Fields stripped from the sync block on every write, and populated into the
# secrets block only when `include_secrets=True` on manual export. Each path
# is a tuple of keys relative to `settings`.
#
# Why these specifically:
#   * whisper.remote_api_key    — credential, full stop.
#   * output.post_process.command, output.pre_output_command,
#     output.post_output_command — each is an arbitrary shell command
#     executed by Voxtype on every transcription. A malicious bundle that
#     set any of these to `bash -c '…'` would achieve RCE on the next
#     recording. Treating them as secrets means (a) they are never
#     silently carried across devices via Syncthing and (b) they require
#     an explicit confirmation on import.
SECRET_PATHS: tuple[tuple[str, ...], ...] = (
    ("whisper", "remote_api_key"),
    ("output", "post_process", "command"),
    ("output", "pre_output_command"),
    ("output", "post_output_command"),
)

# Dangerous-but-not-necessarily-secret: any change to one of these requires
# an explicit import confirmation, regardless of whether the imported value
# is the same as the one already on disk. Superset of SECRET_PATHS plus:
#   * whisper.remote_endpoint — not a credential, but changing it redirects
#     raw audio to a different server. A malicious bundle could quietly
#     set this to an attacker's endpoint for passive audio exfiltration.
DANGEROUS_PATHS: tuple[tuple[str, ...], ...] = SECRET_PATHS + (
    ("whisper", "remote_endpoint"),
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class BundleError(ValueError):
    """Raised when a JSON payload can't be accepted as a bundle."""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class Bundle:
    schema_version: int
    format: str
    generated_at: str
    generated_by_device: str
    local_sync_hash: str
    sync: dict[str, Any]
    local: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, Any] | None = None  # absent on disk when None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "format": self.format,
            "generated_at": self.generated_at,
            "generated_by_device": self.generated_by_device,
            "local_sync_hash": self.local_sync_hash,
            "sync": self.sync,
            "local": self.local,
        }
        if self.secrets is not None:
            out["secrets"] = self.secrets
        return out


# ---------------------------------------------------------------------------
# Utility: safe accessors for tomlkit docs / nested dicts
# ---------------------------------------------------------------------------

def _get(node: Any, *path: str, default: Any = None) -> Any:
    """Traverse a nested mapping via `.get`. Returns `default` if any step
    is missing or the node at that step isn't mapping-like."""
    cur: Any = node
    for key in path:
        if cur is None or not hasattr(cur, "get"):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _as_str(v: Any) -> str | None:
    if v is None:
        return None
    return str(v)


def _as_bool(v: Any) -> bool | None:
    if v is None:
        return None
    try:
        return bool(v)
    except Exception:
        return None


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_str_list(v: Any) -> list[str] | None:
    if v is None:
        return None
    try:
        return [str(item) for item in v]
    except TypeError:
        return None


def _put(dst: dict[str, Any], value: Any, *path: str) -> None:
    """Set `value` at nested `path` in `dst`, creating intermediate dicts.
    No-op when `value is None` — we keep the serialized shape tidy."""
    if value is None:
        return
    node = dst
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


# ---------------------------------------------------------------------------
# Distillation: config.toml + sidecar → bundle blocks
# ---------------------------------------------------------------------------

def distill_sync(
    doc: Any,
    vocabulary: Iterable[VocabEntry],
    replacements: Iterable[ReplacementEntry],
) -> dict[str, Any]:
    """Build the `sync` block from live state. Pure: no I/O, no side effects.

    `doc` is mapping-like (a tomlkit `TOMLDocument` or a plain dict). Values
    are coerced to JSON primitives — tomlkit's `Bool`/`Integer` wrappers
    would otherwise serialize oddly or compare inequal to Python primitives.
    """
    vocab_list = [
        {
            "phrase": v.phrase,
            "added_at": v.added_at,
            "notes": v.notes,
        }
        for v in vocabulary
    ]

    rep_list = [
        {
            "from": r.from_text,
            "to": _get_replacement_to(doc, r.from_text),
            "category": _normalize_category(r.category),
            "added_at": r.added_at,
        }
        for r in replacements
    ]
    # Drop entries whose config-side value has disappeared since reconcile.
    # Shouldn't happen in practice (state keeps these in lockstep) but we
    # defensively skip them rather than emit a null `to`.
    rep_list = [r for r in rep_list if r["to"] is not None]

    settings: dict[str, Any] = {}
    _put(settings, _as_str(doc.get("engine")), "engine")

    # Whisper
    _put(settings, _as_str(_get(doc, "whisper", "model")), "whisper", "model")
    _put(settings, _as_str(_get(doc, "whisper", "backend")), "whisper", "backend")
    _put(settings, _as_str(_get(doc, "whisper", "language")), "whisper", "language")
    _put(settings, _as_bool(_get(doc, "whisper", "translate")), "whisper", "translate")
    _put(settings, _as_int(_get(doc, "whisper", "threads")), "whisper", "threads")
    _put(settings, _as_str(_get(doc, "whisper", "remote_endpoint")), "whisper", "remote_endpoint")
    _put(settings, _as_str(_get(doc, "whisper", "remote_model")), "whisper", "remote_model")
    _put(settings, _as_int(_get(doc, "whisper", "remote_timeout_secs")), "whisper", "remote_timeout_secs")

    # Per-engine model keys. Each engine stores its model under a different
    # field (see settings.MODEL_PATH_PER_ENGINE); we mirror that shape so the
    # applier can write back without a translation table.
    for engine, subkey in (
        ("parakeet", "model_type"),
        ("moonshine", "model"),
        ("sensevoice", "model"),
        ("paraformer", "model"),
        ("dolphin", "model"),
        ("omnilingual", "model"),
    ):
        _put(settings, _as_str(_get(doc, engine, subkey)), engine, subkey)

    # Output (minus the three *_command fields, which are secrets)
    _put(settings, _as_str(_get(doc, "output", "mode")), "output", "mode")
    _put(settings, _as_bool(_get(doc, "output", "auto_submit")), "output", "auto_submit")
    _put(settings, _as_bool(_get(doc, "output", "fallback_to_clipboard")), "output", "fallback_to_clipboard")
    _put(settings, _as_int(_get(doc, "output", "type_delay_ms")), "output", "type_delay_ms")
    _put(settings, _as_int(_get(doc, "output", "post_process", "timeout_ms")),
         "output", "post_process", "timeout_ms")

    # Text-processing toggles
    _put(settings, _as_bool(_get(doc, "text", "spoken_punctuation")), "text", "spoken_punctuation")
    _put(settings, _as_bool(_get(doc, "text", "smart_auto_submit")), "text", "smart_auto_submit")

    return {
        "vocabulary": vocab_list,
        "replacements": rep_list,
        "settings": settings,
    }


def distill_local(doc: Any) -> dict[str, Any]:
    """Per-device settings. Never auto-applied on sync; included in bundles
    only for the "full backup / migrate to new machine" workflow."""
    out: dict[str, Any] = {}

    _put(out, _as_str(doc.get("state_file")), "state_file")

    # Hotkey — keyboard-dependent; user confirmed this stays local.
    _put(out, _as_str(_get(doc, "hotkey", "key")), "hotkey", "key")
    _put(out, _as_str_list(_get(doc, "hotkey", "modifiers")), "hotkey", "modifiers")
    _put(out, _as_str(_get(doc, "hotkey", "mode")), "hotkey", "mode")
    _put(out, _as_bool(_get(doc, "hotkey", "enabled")), "hotkey", "enabled")

    # Audio — hardware-dependent.
    _put(out, _as_str(_get(doc, "audio", "device")), "audio", "device")
    _put(out, _as_int(_get(doc, "audio", "sample_rate")), "audio", "sample_rate")
    _put(out, _as_int(_get(doc, "audio", "max_duration_secs")), "audio", "max_duration_secs")
    _put(out, _as_bool(_get(doc, "audio", "feedback", "enabled")), "audio", "feedback", "enabled")
    _put(out, _as_str(_get(doc, "audio", "feedback", "theme")), "audio", "feedback", "theme")
    _put(out, _as_float(_get(doc, "audio", "feedback", "volume")), "audio", "feedback", "volume")

    # VAD — mic-dependent tuning.
    _put(out, _as_bool(_get(doc, "vad", "enabled")), "vad", "enabled")
    _put(out, _as_str(_get(doc, "vad", "model")), "vad", "model")
    _put(out, _as_float(_get(doc, "vad", "threshold")), "vad", "threshold")

    return out


def distill_secrets(doc: Any) -> dict[str, Any]:
    """Extract the fields listed in SECRET_PATHS into an isolated block.

    Only non-empty values end up in the result. An empty `remote_api_key`
    string (common when the user wires the key via the environment
    variable instead) is treated as absent, not as an empty secret.
    """
    out: dict[str, Any] = {}
    for path in SECRET_PATHS:
        val = _get(doc, *path)
        s = _as_str(val)
        if s is None or s == "":
            continue
        _put(out, s, *path)
    return out


def _normalize_category(cat: str) -> str:
    cat = _LEGACY_CATEGORY_MIGRATIONS.get(cat, cat)
    return cat if cat in CATEGORIES else DEFAULT_CATEGORY


def _get_replacement_to(doc: Any, from_text: str) -> str | None:
    reps = _get(doc, "text", "replacements")
    if reps is None:
        return None
    val = reps.get(from_text)
    return _as_str(val)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def _canonicalize(value: Any) -> Any:
    """Recursively sort keys and normalize container types for hashing.

    Lists are preserved in order — both vocabulary and replacements are
    user-ordered and that order can matter (Whisper attends slightly more
    to prompt-leading tokens). Two bundles with the same items in different
    order therefore hash differently; that's correct.
    """
    if isinstance(value, Mapping):
        return {k: _canonicalize(value[k]) for k in sorted(value.keys())}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    return value


def stable_hash(sync_block: Mapping[str, Any]) -> str:
    """Deterministic sha256 of the sync block. Stable across Python runs,
    across machines, across key-insertion order. 64-char hex."""
    canonical = _canonicalize(sync_block)
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,  # redundant with _canonicalize; belt-and-suspenders
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Build / serialize / parse
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_bundle(
    *,
    sync: dict[str, Any],
    local: dict[str, Any],
    secrets: dict[str, Any] | None,
    device_label: str,
    include_secrets: bool,
    generated_at: str | None = None,
) -> Bundle:
    """Assemble a Bundle from pre-distilled blocks.

    The caller decides whether to pass `secrets` in (only do so when the
    user has explicitly opted into "Include secrets" for a manual export).
    `include_secrets=False` hard-strips the block regardless of what was
    passed — this is the belt-and-suspenders guarantee that auto-sync
    writes can never leak credentials even if the caller is buggy.
    """
    if not isinstance(device_label, str) or not device_label.strip():
        raise BundleError("device_label must be a non-empty string")
    if len(device_label) > MAX_DEVICE_LABEL_LEN:
        raise BundleError(
            f"device_label exceeds {MAX_DEVICE_LABEL_LEN} characters"
        )

    # Explicit None-check rather than truthiness: an empty dict is a
    # meaningful marker in manual exports ("the dialog asked about
    # secrets; there weren't any") and must survive to the output,
    # whereas `include_secrets=False` unconditionally suppresses the
    # block (auto-sync invariant).
    if include_secrets:
        actual_secrets = secrets if secrets is not None else {}
    else:
        actual_secrets = None
    return Bundle(
        schema_version=SCHEMA_VERSION,
        format=FORMAT_TAG,
        generated_at=generated_at or _iso_now(),
        generated_by_device=device_label.strip(),
        local_sync_hash=stable_hash(sync),
        sync=sync,
        local=local,
        secrets=actual_secrets,
    )


def to_json(bundle: Bundle, *, indent: int | None = 2) -> str:
    """Serialize a Bundle to a human-readable (indented) JSON string.

    Default `indent=2` matches the sidecar's style and keeps the file
    diff-friendly under Syncthing. Pass `indent=None` for compact form.
    """
    return json.dumps(bundle.to_dict(), ensure_ascii=False, indent=indent, sort_keys=False)


def from_json(text: str) -> Bundle:
    """Parse a JSON payload as a voxtype-tui bundle.

    Raises BundleError with a human-readable message on every validation
    failure. The caller is expected to surface the message in a banner.

    Validation does NOT verify semantic correctness of individual settings
    — that's the applier's job, and it needs access to the live config
    anyway. Here we only enforce shape, size, schema version, and the
    flat length caps that protect the UI.
    """
    if not isinstance(text, (str, bytes)):
        raise BundleError("bundle payload must be text")
    payload_bytes = text.encode("utf-8") if isinstance(text, str) else text
    if len(payload_bytes) > MAX_BUNDLE_BYTES:
        raise BundleError(
            f"bundle is {len(payload_bytes)} bytes; refusing to parse "
            f"(limit {MAX_BUNDLE_BYTES})"
        )

    try:
        parsed = json.loads(payload_bytes)
    except json.JSONDecodeError as e:
        raise BundleError(f"invalid JSON: {e}") from e

    if not isinstance(parsed, dict):
        raise BundleError("bundle root must be a JSON object")

    fmt = parsed.get("format")
    if fmt != FORMAT_TAG:
        raise BundleError(
            f"unknown format {fmt!r}; expected {FORMAT_TAG!r}"
        )

    schema = parsed.get("schema_version")
    if not isinstance(schema, int):
        raise BundleError("schema_version must be an integer")
    if schema > SCHEMA_VERSION:
        raise BundleError(
            f"bundle schema_version {schema} is newer than this app "
            f"understands ({SCHEMA_VERSION}). Update voxtype-tui to import."
        )
    if schema < 1:
        raise BundleError(f"schema_version {schema} is not supported")

    sync = parsed.get("sync")
    if not isinstance(sync, dict):
        raise BundleError("bundle is missing a `sync` object")

    local = parsed.get("local", {})
    if not isinstance(local, dict):
        raise BundleError("`local` must be a JSON object when present")

    secrets = parsed.get("secrets")
    if secrets is not None and not isinstance(secrets, dict):
        raise BundleError("`secrets` must be a JSON object when present")

    _validate_sync_block(sync)

    device_label = str(parsed.get("generated_by_device") or "")
    if len(device_label) > MAX_DEVICE_LABEL_LEN:
        raise BundleError(
            f"generated_by_device exceeds {MAX_DEVICE_LABEL_LEN} characters"
        )

    return Bundle(
        schema_version=schema,
        format=fmt,
        generated_at=str(parsed.get("generated_at") or ""),
        generated_by_device=device_label,
        local_sync_hash=str(parsed.get("local_sync_hash") or ""),
        sync=sync,
        local=local,
        secrets=secrets,
    )


def _validate_sync_block(sync: dict[str, Any]) -> None:
    vocab = sync.get("vocabulary", [])
    if not isinstance(vocab, list):
        raise BundleError("sync.vocabulary must be a list")
    if len(vocab) > MAX_VOCAB_COUNT:
        raise BundleError(
            f"sync.vocabulary has {len(vocab)} entries; limit {MAX_VOCAB_COUNT}"
        )
    for i, v in enumerate(vocab):
        if not isinstance(v, dict):
            raise BundleError(f"sync.vocabulary[{i}] is not an object")
        phrase = v.get("phrase")
        if not isinstance(phrase, str) or not phrase:
            raise BundleError(f"sync.vocabulary[{i}].phrase must be a non-empty string")
        if len(phrase) > MAX_VOCAB_PHRASE_LEN:
            raise BundleError(
                f"sync.vocabulary[{i}].phrase exceeds {MAX_VOCAB_PHRASE_LEN} chars"
            )

    reps = sync.get("replacements", [])
    if not isinstance(reps, list):
        raise BundleError("sync.replacements must be a list")
    if len(reps) > MAX_REPLACEMENT_COUNT:
        raise BundleError(
            f"sync.replacements has {len(reps)} entries; limit {MAX_REPLACEMENT_COUNT}"
        )
    for i, r in enumerate(reps):
        if not isinstance(r, dict):
            raise BundleError(f"sync.replacements[{i}] is not an object")
        for key in ("from", "to"):
            val = r.get(key)
            if not isinstance(val, str) or not val:
                raise BundleError(
                    f"sync.replacements[{i}].{key} must be a non-empty string"
                )
            if len(val) > MAX_REPLACEMENT_LEN:
                raise BundleError(
                    f"sync.replacements[{i}].{key} exceeds {MAX_REPLACEMENT_LEN} chars"
                )

    settings = sync.get("settings", {})
    if not isinstance(settings, dict):
        raise BundleError("sync.settings must be a JSON object")
    _walk_check_string_lengths(settings, prefix="sync.settings")


def _walk_check_string_lengths(node: Any, *, prefix: str) -> None:
    """Defensive: any string setting over MAX_SETTING_STRING_LEN is rejected.
    Prevents a crafted bundle from planting a 5 MB path or command string."""
    if isinstance(node, str):
        if len(node) > MAX_SETTING_STRING_LEN:
            raise BundleError(
                f"{prefix} string exceeds {MAX_SETTING_STRING_LEN} chars"
            )
    elif isinstance(node, dict):
        for k, v in node.items():
            _walk_check_string_lengths(v, prefix=f"{prefix}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_check_string_lengths(v, prefix=f"{prefix}[{i}]")


# ---------------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------------

def redact_secrets_dict(secrets: dict[str, Any]) -> dict[str, Any]:
    """Return a secrets dict with all known-secret values replaced by ``""``.

    Manual export with Redact=True wants the exported file to SHOW that
    these fields were intentionally blanked out — an empty string at the
    right path reads differently from a missing key. On import:

      * blank (``""``) → "don't overwrite local value" (see
        ``diff_dangerous``; redacted imports are no-ops for secrets)
      * missing key → also "don't overwrite"; equivalent on import, but
        visually louder in the exported file that the export WAS asked
        for and secrets WERE present at the time, just not copied out.

    Paths not present in the input are left out — we never synthesize
    keys the user never set.
    """
    result: dict[str, Any] = {}
    for path in SECRET_PATHS:
        val = _resolve(secrets, path)
        if val is None:
            continue
        _put(result, "", *path)
    return result


def strip_secrets(bundle: Bundle) -> Bundle:
    """Return a copy of `bundle` with its secrets block removed.

    Useful for tests, for the Syncthing writer (which should go through
    `build_bundle(include_secrets=False)` instead but double-stripping
    is safe), and as a utility when a user re-exports a previously
    secret-bearing file without secrets.
    """
    return Bundle(
        schema_version=bundle.schema_version,
        format=bundle.format,
        generated_at=bundle.generated_at,
        generated_by_device=bundle.generated_by_device,
        local_sync_hash=bundle.local_sync_hash,
        sync=bundle.sync,
        local=bundle.local,
        secrets=None,
    )


def diff_dangerous(
    current_sync: dict[str, Any],
    imported_sync: dict[str, Any],
    current_secrets: dict[str, Any] | None,
    imported_secrets: dict[str, Any] | None,
) -> list[str]:
    """Return the dangerous-path dotted names whose values differ between
    the current state and the imported bundle.

    Used by the import preview modal: any path returned here must be
    confirmed explicitly by the user before apply. Paths that are in the
    imported bundle as redacted (missing / empty-string) are treated as
    "don't change" — they show up here only if the imported VALUE differs
    from the current one.
    """
    differences: list[str] = []
    current_settings = current_sync.get("settings", {}) if isinstance(current_sync, dict) else {}
    imported_settings = imported_sync.get("settings", {}) if isinstance(imported_sync, dict) else {}

    for path in DANGEROUS_PATHS:
        cur = _resolve(current_settings, path)
        new = _resolve(imported_settings, path)
        # Secret paths also appear in the secrets block — pull from there
        # when the setting side is empty. Import preview must surface a
        # credential change that came through the secrets channel.
        if path in SECRET_PATHS:
            cur_secret = _resolve(current_secrets or {}, path)
            new_secret = _resolve(imported_secrets or {}, path)
            if cur is None or cur == "":
                cur = cur_secret
            if new is None or new == "":
                new = new_secret
        # Redacted-on-import (None or empty string) = "don't change local",
        # not "change to empty". Skip.
        if new is None or new == "":
            continue
        if cur != new:
            differences.append(".".join(path))
    return differences


def _resolve(node: Any, path: tuple[str, ...]) -> Any:
    cur = node
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


# ---------------------------------------------------------------------------
# Vexis import adapter
# ---------------------------------------------------------------------------

VEXIS_DICTIONARY_FORMAT = "vexis-dictionary"
VEXIS_VOCABULARY_FORMAT = "vexis-vocabulary"
VOXTYPE_TUI_FORMAT = "voxtype-tui"
UNKNOWN_FORMAT = "unknown"


def detect_format(parsed: Any) -> str:
    """Identify the shape of a parsed JSON payload.

    Vexis ships two separate files (vexis-dictionary.json /
    vexis-vocabulary.json) so we accept them independently — the user
    picks the relevant file, we detect its format, adapt, and apply.
    """
    if isinstance(parsed, dict) and parsed.get("format") == FORMAT_TAG:
        return VOXTYPE_TUI_FORMAT
    if isinstance(parsed, list):
        if not parsed:
            # Empty array is ambiguous; treat as vocabulary (the simpler
            # shape). No entries means no changes anyway.
            return VEXIS_VOCABULARY_FORMAT
        first = parsed[0]
        if isinstance(first, str):
            return VEXIS_VOCABULARY_FORMAT
        if isinstance(first, dict):
            if "trigger" in first and "replacement" in first:
                return VEXIS_DICTIONARY_FORMAT
            if "word" in first:
                return VEXIS_VOCABULARY_FORMAT
    return UNKNOWN_FORMAT


# Vexis serializes its category enum as lowercase strings.
_VEXIS_CATEGORY_MAP: dict[str, str] = {
    "replacement": "Replacement",
    "capitalization": "Capitalization",
    # Vexis's own importer migrates `command` → `replacement`; we do the
    # same so a legacy Vexis export lands in the right bucket for us.
    "command": "Replacement",
}


def adapt_vexis_dictionary(parsed: Any) -> list[dict[str, Any]]:
    """Convert a Vexis dictionary export into voxtype-tui replacement dicts.

    Output shape matches the `sync.replacements` schema — plug directly
    into a bundle after distillation. Duplicates are deduped
    case-insensitively on the trigger (last wins), mirroring Vexis's own
    import semantics.
    """
    if not isinstance(parsed, list):
        raise BundleError("Vexis dictionary export must be a JSON array")
    if len(parsed) > MAX_REPLACEMENT_COUNT:
        raise BundleError(
            f"Vexis dictionary has {len(parsed)} entries; limit {MAX_REPLACEMENT_COUNT}"
        )
    by_key: dict[str, dict[str, Any]] = {}
    for i, row in enumerate(parsed):
        if not isinstance(row, dict):
            raise BundleError(f"Vexis dictionary row {i} is not an object")
        trigger = row.get("trigger")
        replacement = row.get("replacement")
        if not isinstance(trigger, str) or not trigger.strip():
            raise BundleError(f"Vexis dictionary row {i} has no trigger")
        if not isinstance(replacement, str) or not replacement:
            raise BundleError(f"Vexis dictionary row {i} has no replacement")
        if len(trigger) > MAX_REPLACEMENT_LEN or len(replacement) > MAX_REPLACEMENT_LEN:
            raise BundleError(
                f"Vexis dictionary row {i} exceeds {MAX_REPLACEMENT_LEN} chars"
            )
        raw_cat = row.get("category")
        cat = _VEXIS_CATEGORY_MAP.get(
            raw_cat.lower() if isinstance(raw_cat, str) else "",
            DEFAULT_CATEGORY,
        )
        key = trigger.strip().lower()
        by_key[key] = {
            "from": trigger.strip(),
            "to": replacement,
            "category": cat,
            "added_at": _iso_now(),
        }
    return list(by_key.values())


def adapt_vexis_vocabulary(parsed: Any) -> list[dict[str, Any]]:
    """Convert a Vexis vocabulary export into voxtype-tui vocab dicts.

    Accepts either shape the Vexis frontend produces:
      * a plain list of strings
      * a list of {id, word} objects
    Dedupes case-insensitively on phrase (first wins, matching Vexis's
    UNIQUE COLLATE NOCASE constraint on the `word` column).
    """
    if not isinstance(parsed, list):
        raise BundleError("Vexis vocabulary export must be a JSON array")
    if len(parsed) > MAX_VOCAB_COUNT:
        raise BundleError(
            f"Vexis vocabulary has {len(parsed)} entries; limit {MAX_VOCAB_COUNT}"
        )
    words: list[str] = []
    seen: set[str] = set()
    for i, row in enumerate(parsed):
        if isinstance(row, str):
            phrase = row
        elif isinstance(row, dict):
            phrase = row.get("word")
            if not isinstance(phrase, str):
                raise BundleError(f"Vexis vocabulary row {i} missing `word`")
        else:
            raise BundleError(f"Vexis vocabulary row {i} has unexpected shape")
        phrase = phrase.strip()
        if not phrase:
            continue
        if len(phrase) > MAX_VOCAB_PHRASE_LEN:
            raise BundleError(
                f"Vexis vocabulary row {i} exceeds {MAX_VOCAB_PHRASE_LEN} chars"
            )
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        words.append(phrase)
    now = _iso_now()
    return [{"phrase": w, "added_at": now, "notes": None} for w in words]


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def estimate_initial_prompt_tokens(phrases: Iterable[str]) -> int:
    """Rough token count for the joined vocab string. ~4 chars/token. Used
    to decide whether to surface a "close to Whisper's 224-token cap"
    warning on import."""
    joined = ", ".join(phrases)
    if not joined:
        return 0
    return max(1, (len(joined) + APPROX_CHARS_PER_TOKEN - 1) // APPROX_CHARS_PER_TOKEN)


def exceeds_initial_prompt_limit(phrases: Iterable[str]) -> bool:
    return estimate_initial_prompt_tokens(phrases) > WHISPER_INITIAL_PROMPT_TOKEN_LIMIT


# ---------------------------------------------------------------------------
# Writer: atomic, idempotent sync.json generation
# ---------------------------------------------------------------------------

def get_device_label() -> str:
    """Best-effort device identifier for `generated_by_device`.

    `socket.gethostname()` is the default; any unexpected failure falls back
    to `"unknown-device"` rather than propagating. The reader's banner shows
    this string verbatim ("Synced from {label}"), so empty is worse than
    generic.

    A future Settings screen may let the user override this value (Step
    5+); for now it's hostname, period.
    """
    try:
        host = socket.gethostname().strip()
    except Exception:
        return "unknown-device"
    return host or "unknown-device"


def write_sync_bundle(
    doc: Any,
    sc: Sidecar,
    path: Path | None = None,
    *,
    device_label: str | None = None,
) -> bool:
    """Distill the live state, hash-compare against the file on disk, and
    atomically (re)write `sync.json` only when the content actually changed.

    Returns `True` if the file was written, `False` if the existing file's
    embedded `local_sync_hash` already matched and the write was skipped.
    The return value is primarily a test hook; application code can ignore
    it.

    Failure modes:
      * Any OSError during the actual write propagates to the caller —
        the save path treats a failed sync write as non-fatal, but this
        function stays honest about what happened.
      * A corrupt or partially-written existing `sync.json` never blocks
        a fresh write. We log-and-continue on JSONDecodeError / OSError
        during the read-for-compare step.
      * Never writes if `sync.json`'s current content already matches —
        avoids Syncthing thrash on every unrelated local edit (e.g. the
        user flipping `audio.feedback.volume` triggers a save but the
        sync block is unchanged, so sync.json's mtime stays stable).

    This function must only be called AFTER the primary `config.toml` +
    sidecar saves succeed. Order matters: sync.json advertising state
    that didn't actually commit would be a correctness footgun (another
    device could pull the phantom state before we realize the local save
    failed). Callers enforce the ordering — this function is purely the
    writer stage.
    """
    # Resolve the default lazily so tests can monkeypatch `sync.SYNC_PATH`
    # and have it take effect without needing to thread the path through
    # every caller (notably `AppState.save`, which calls us with no path).
    if path is None:
        path = SYNC_PATH
    bundle = build_bundle(
        sync=distill_sync(doc, sc.vocabulary, sc.replacements),
        local=distill_local(doc),
        secrets=None,
        device_label=device_label or get_device_label(),
        include_secrets=False,
    )
    new_hash = bundle.local_sync_hash

    if _existing_hash_matches(path, new_hash):
        return False

    _atomic_write_text(path, to_json(bundle))
    return True


async def write_sync_bundle_async(
    doc: Any,
    sc: Sidecar,
    path: Path | None = None,
    *,
    device_label: str | None = None,
) -> bool:
    """Async wrapper for callers on the Textual event loop. The actual
    work runs on a worker thread so neither the read-for-compare nor
    the atomic rename blocks the UI."""
    return await asyncio.to_thread(
        write_sync_bundle, doc, sc, path, device_label=device_label,
    )


# ---------------------------------------------------------------------------
# Manual export: user-initiated, policy-flexible bundle write
# ---------------------------------------------------------------------------

SCOPE_SYNC_ONLY = "sync"
SCOPE_SYNC_PLUS_LOCAL = "sync+local"
EXPORT_SCOPES = (SCOPE_SYNC_ONLY, SCOPE_SYNC_PLUS_LOCAL)


def default_export_filename(today: datetime | None = None) -> str:
    """Suggested leaf name used by the export dialog.

    `today` is injectable for tests; production callers pass nothing.
    """
    stamp = (today or datetime.now()).strftime("%Y-%m-%d")
    return f"voxtype-tui-export-{stamp}.json"


def default_export_path() -> Path:
    """Suggested absolute path: `~/Downloads/<filename>`. Downloads is the
    convention the user already thinks in ("where files go when I ask
    for something to be saved"). Falls back to the home directory if
    Downloads doesn't exist — we never create it ourselves."""
    downloads = Path.home() / "Downloads"
    base = downloads if downloads.exists() else Path.home()
    return base / default_export_filename()


def build_export_bundle(
    doc: Any,
    sc: Sidecar,
    *,
    scope: str,
    redact_secrets: bool,
    device_label: str | None = None,
) -> Bundle:
    """Build a bundle for manual export, honoring the scope and redact knobs.

    Two orthogonal knobs:

    * ``scope``: ``SCOPE_SYNC_ONLY`` → `local` block is empty dict.
      ``SCOPE_SYNC_PLUS_LOCAL`` → `local` block populated from doc.
      Sync-only is the safer default for a "share these settings with my
      other laptop" workflow; sync+local is the "full backup before
      wipe" case.

    * ``redact_secrets``:

      - ``True``  → secrets block contains SECRET_PATHS with empty-string
        values (see ``redact_secrets_dict``). Exported file shows "I was
        asked about secrets but chose not to share them"; on import
        these paths become no-ops.
      - ``False`` → secrets block contains real credential / command
        strings verbatim. Use this only when you trust the destination
        (e.g. personal backup drive; encrypted volume). Surface a
        confirmation in the UI before accepting this choice.

    Manual export ALWAYS includes a secrets block (redacted or not). The
    auto-sync writer (`write_sync_bundle`) is the only caller that omits
    the block entirely — absence is the signal that a sync.json file is
    guaranteed secret-free.
    """
    if scope not in EXPORT_SCOPES:
        raise BundleError(f"unknown export scope: {scope!r}")

    sync_block = distill_sync(doc, sc.vocabulary, sc.replacements)
    local_block = distill_local(doc) if scope == SCOPE_SYNC_PLUS_LOCAL else {}
    secrets_source = distill_secrets(doc)
    if redact_secrets:
        secrets_block: dict[str, Any] = redact_secrets_dict(secrets_source)
    else:
        secrets_block = secrets_source

    return build_bundle(
        sync=sync_block,
        local=local_block,
        # Always pass secrets through (possibly redacted / possibly
        # empty). `include_secrets=True` keeps the block in the output
        # even when it's an empty dict — manual exports benefit from a
        # visible (even if empty) block so the user can tell at a glance
        # that the file was produced with the manual-export dialog.
        secrets=secrets_block,
        device_label=device_label or get_device_label(),
        include_secrets=True,
    )


# ---------------------------------------------------------------------------
# Manual import: foreign / native bundle → merged state
# ---------------------------------------------------------------------------

@dataclass
class VocabDiff:
    added: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


@dataclass
class ReplacementDiff:
    added: list[tuple[str, str]] = field(default_factory=list)  # (from, to)
    updated: list[tuple[str, str, str]] = field(default_factory=list)
    # (from, old_to, new_to)
    unchanged: list[str] = field(default_factory=list)


@dataclass
class SettingChange:
    path: str
    old: Any
    new: Any
    dangerous: bool = False


@dataclass
class ImportPreview:
    """Structured diff between an imported bundle and current state.

    The import modal renders this as human-readable bullets. `dangerous`
    entries in `settings` are also rendered verbatim so the user can
    read the exact API key / shell command before accepting — the
    preview is the last line of defense against a malicious bundle.
    """
    source: str
    vocab: VocabDiff = field(default_factory=VocabDiff)
    replacements: ReplacementDiff = field(default_factory=ReplacementDiff)
    settings: list[SettingChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_bundle_file(path: Path) -> tuple[Bundle, list[str]]:
    """Read a JSON file at `path` and return (bundle, warnings).

    Accepts three formats:
      * voxtype-tui native (our own export / sync.json)
      * Vexis dictionary (list of {trigger, replacement, category})
      * Vexis vocabulary (list of strings OR list of {id, word})

    Vexis formats are wrapped into a synthetic voxtype-tui Bundle so
    downstream logic (preview, apply) doesn't need to branch. The
    `generated_by_device` field is set to the Vexis format name so the
    preview modal can label the source honestly.

    Raises BundleError with a user-readable message on any failure —
    oversize file, invalid JSON, unrecognized shape, schema too new,
    etc. Callers should surface the message in the modal and keep the
    dialog open for retry.
    """
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise BundleError(f"could not read file: {e}") from e
    if len(raw) > MAX_BUNDLE_BYTES:
        raise BundleError(
            f"file is {len(raw)} bytes; limit {MAX_BUNDLE_BYTES}"
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BundleError(f"invalid JSON: {e}") from e

    fmt = detect_format(parsed)
    warnings: list[str] = []

    if fmt == VOXTYPE_TUI_FORMAT:
        # Delegate to from_json for full schema + length validation.
        return from_json(raw.decode("utf-8")), warnings

    if fmt == VEXIS_DICTIONARY_FORMAT:
        reps = adapt_vexis_dictionary(parsed)
        warnings.append(
            f"Imported Vexis dictionary format: {len(reps)} replacement(s)."
        )
        return _synthesize_bundle_from_vexis(
            vocabulary=[], replacements=reps, source=VEXIS_DICTIONARY_FORMAT,
        ), warnings

    if fmt == VEXIS_VOCABULARY_FORMAT:
        vocab = adapt_vexis_vocabulary(parsed)
        warnings.append(
            f"Imported Vexis vocabulary format: {len(vocab)} phrase(s)."
        )
        return _synthesize_bundle_from_vexis(
            vocabulary=vocab, replacements=[], source=VEXIS_VOCABULARY_FORMAT,
        ), warnings

    raise BundleError(
        "Unrecognized file format. Expected a voxtype-tui bundle or a "
        "Vexis export (vexis-dictionary.json / vexis-vocabulary.json)."
    )


def _synthesize_bundle_from_vexis(
    vocabulary: list[dict[str, Any]],
    replacements: list[dict[str, Any]],
    source: str,
) -> Bundle:
    """Wrap Vexis-adapted data in a synthetic voxtype-tui Bundle so the
    preview/apply pipeline is single-path."""
    sync_block: dict[str, Any] = {
        "vocabulary": vocabulary,
        "replacements": replacements,
        "settings": {},
    }
    return Bundle(
        schema_version=SCHEMA_VERSION,
        format=FORMAT_TAG,
        generated_at=_iso_now(),
        generated_by_device=source,
        local_sync_hash=stable_hash(sync_block),
        sync=sync_block,
        local={},
        secrets=None,
    )


def diff_bundle_against_state(
    bundle: Bundle,
    doc: Any,
    sc: Sidecar,
    *,
    include_local: bool = False,
) -> ImportPreview:
    """Compute what applying `bundle` would change vs current state.

    Pure: no mutations. `include_local=True` also diffs the `local`
    block (matches the "import local settings too" checkbox in the
    preview modal).
    """
    preview = ImportPreview(source=bundle.generated_by_device or bundle.format)

    # --- Vocabulary
    existing_phrases = {v.phrase for v in sc.vocabulary}
    for entry in bundle.sync.get("vocabulary", []):
        phrase = entry.get("phrase")
        if not isinstance(phrase, str) or not phrase:
            continue
        if phrase in existing_phrases:
            preview.vocab.unchanged.append(phrase)
        else:
            preview.vocab.added.append(phrase)

    # --- Replacements
    current_reps: dict[str, str] = {}
    text_node = doc.get("text") if hasattr(doc, "get") else None
    if text_node is not None:
        rep_table = text_node.get("replacements") if hasattr(text_node, "get") else None
        if rep_table is not None:
            for k, v in rep_table.items():
                current_reps[str(k)] = str(v)
    for entry in bundle.sync.get("replacements", []):
        frm = entry.get("from")
        to = entry.get("to")
        if not isinstance(frm, str) or not isinstance(to, str):
            continue
        if frm not in current_reps:
            preview.replacements.added.append((frm, to))
        elif current_reps[frm] != to:
            preview.replacements.updated.append((frm, current_reps[frm], to))
        else:
            preview.replacements.unchanged.append(frm)

    # --- Settings (sync + optionally local)
    settings_changes = _diff_settings(
        current=doc,
        incoming=bundle.sync.get("settings", {}),
    )
    if include_local:
        settings_changes.extend(_diff_settings(
            current=doc,
            incoming=bundle.local or {},
        ))

    # --- Secrets: only non-blank values propose a change
    if bundle.secrets:
        settings_changes.extend(_diff_settings(
            current=doc,
            incoming=bundle.secrets,
            skip_empty_strings=True,
        ))

    preview.settings = settings_changes
    return preview


def _diff_settings(
    current: Any,
    incoming: dict[str, Any],
    *,
    skip_empty_strings: bool = False,
    path: tuple[str, ...] = (),
) -> list[SettingChange]:
    out: list[SettingChange] = []
    for key, new_value in incoming.items():
        current_path = path + (key,)
        if isinstance(new_value, dict):
            out.extend(_diff_settings(
                current=current,
                incoming=new_value,
                skip_empty_strings=skip_empty_strings,
                path=current_path,
            ))
            continue
        if skip_empty_strings and new_value == "":
            continue
        old_value = _resolve(current, current_path)
        # tomlkit's String / Integer / Bool wrap raw values; coerce to
        # Python primitives for an honest comparison. Otherwise
        # `tomlkit.Integer(5) != 5` would produce a false-positive diff.
        old_coerced = _coerce_toml_scalar(old_value)
        if old_coerced == new_value:
            continue
        out.append(SettingChange(
            path=".".join(current_path),
            old=old_coerced,
            new=new_value,
            dangerous=current_path in DANGEROUS_PATHS,
        ))
    return out


def _coerce_toml_scalar(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return float(v)
    if isinstance(v, str):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [_coerce_toml_scalar(x) for x in v]
    return v


def apply_bundle_to_state(
    bundle: Bundle,
    doc: Any,
    sc: Sidecar,
    *,
    include_local: bool = False,
) -> list[str]:
    """Merge bundle contents into `doc` + `sc` in place.

    Merge semantics (not replace):
      * Vocabulary: imported phrases APPENDED if not already present;
        existing phrases kept. `initial_prompt` rebuilt to include all.
      * Replacements: imported `from→to` upserted. Existing replacements
        not in import are kept. Category may update.
      * Settings: field-by-field overwrite for sync block. Each leaf
        imported by the bundle replaces the corresponding doc value.
        `None` / missing leaves are no-ops (preserve local).
      * Local: only applied when `include_local=True`.
      * Secrets: non-blank values overwrite; blank ("") values skipped
        (see `diff_dangerous` redaction contract).

    Does NOT call save. The caller owns when to persist — typically the
    user reviews the merged shadow state in the tabs and hits Ctrl+S
    explicitly.

    Returns a list of human-readable warnings (e.g. initial_prompt
    would exceed Whisper's token budget). Empty list on clean imports.
    """
    warnings: list[str] = []

    # --- Vocabulary: append missing phrases (preserve existing order)
    existing_phrases = {v.phrase for v in sc.vocabulary}
    for entry in bundle.sync.get("vocabulary", []):
        phrase = entry.get("phrase")
        if not isinstance(phrase, str) or not phrase:
            continue
        if phrase in existing_phrases:
            continue
        sc.vocabulary.append(VocabEntry(
            phrase=phrase,
            added_at=entry.get("added_at") or _iso_now(),
            notes=entry.get("notes"),
        ))
        existing_phrases.add(phrase)

    # Rewrite initial_prompt to match the new vocabulary. Do this via
    # tomlkit tables so the existing file's comments survive.
    phrases = [v.phrase for v in sc.vocabulary]
    if phrases:
        _set_nested(doc, ", ".join(phrases), "whisper", "initial_prompt")
    elif _resolve(doc, ("whisper", "initial_prompt")) is not None:
        # Only delete if present — avoid creating an empty [whisper]
        # table on a minimal config.
        try:
            del doc["whisper"]["initial_prompt"]
        except Exception:
            pass
    if exceeds_initial_prompt_limit(phrases):
        warnings.append(
            "Vocabulary is close to Whisper's prompt limit; transcription "
            "quality may degrade beyond ~224 tokens."
        )

    # --- Replacements: upsert into config.text.replacements + sidecar
    rep_node = _resolve(doc, ("text", "replacements"))
    # Snapshot current values so we can compare and choose between
    # creating a fresh inline table vs. incremental updates.
    current_reps: dict[str, str] = {}
    if rep_node is not None:
        for k, v in rep_node.items():
            current_reps[str(k)] = str(v)

    existing_from = {r.from_text for r in sc.replacements}
    for entry in bundle.sync.get("replacements", []):
        frm = entry.get("from")
        to = entry.get("to")
        if not isinstance(frm, str) or not isinstance(to, str):
            continue
        current_reps[frm] = to
        new_cat = _normalize_category(
            entry.get("category", DEFAULT_CATEGORY) or DEFAULT_CATEGORY
        )
        if frm in existing_from:
            for r in sc.replacements:
                if r.from_text == frm and r.category != new_cat:
                    r.category = new_cat
                    break
        else:
            sc.replacements.append(ReplacementEntry(
                from_text=frm,
                category=new_cat,
                added_at=entry.get("added_at") or _iso_now(),
            ))
            existing_from.add(frm)

    # Write merged replacements back into the doc.
    if current_reps:
        _write_replacements_table(doc, current_reps)

    # --- Settings (sync block)
    _apply_settings_dict(doc, bundle.sync.get("settings", {}))

    # --- Local (opt-in)
    if include_local:
        _apply_settings_dict(doc, bundle.local or {})

    # --- Secrets (non-blank only)
    if bundle.secrets:
        _apply_settings_dict(doc, bundle.secrets, skip_empty_strings=True)

    return warnings


def _set_nested(doc: Any, value: Any, *path: str) -> None:
    """Set `value` at nested `path` in a tomlkit document, creating
    intermediate tables via `tomlkit.table()`. Skips the write if the
    current value already equals the new value (avoids spurious
    `loaded_dump != current` dirty flagging)."""
    node = doc
    for key in path[:-1]:
        if key not in node:
            node[key] = tomlkit.table()
        node = node[key]
    last = path[-1]
    current = _coerce_toml_scalar(node.get(last)) if hasattr(node, "get") else None
    if current == value:
        return
    node[last] = value


def _apply_settings_dict(
    doc: Any,
    incoming: dict[str, Any],
    *,
    skip_empty_strings: bool = False,
    path: tuple[str, ...] = (),
) -> None:
    """Walk `incoming` recursively and write each leaf into `doc`."""
    for key, value in incoming.items():
        current_path = path + (key,)
        if isinstance(value, dict):
            _apply_settings_dict(
                doc, value,
                skip_empty_strings=skip_empty_strings,
                path=current_path,
            )
            continue
        if value is None:
            continue
        if skip_empty_strings and value == "":
            continue
        _set_nested(doc, value, *current_path)


def _write_replacements_table(doc: Any, reps: dict[str, str]) -> None:
    """Replace `[text.replacements]` in `doc` with exactly `reps`. Uses
    tomlkit's inline-table form to match the template's commented-out
    hint; users who prefer multi-line tables can flip it after import."""
    if "text" not in doc:
        doc["text"] = tomlkit.table()
    text = doc["text"]
    inline = tomlkit.inline_table()
    for k, v in reps.items():
        inline[k] = v
    text["replacements"] = inline


# ---------------------------------------------------------------------------
# Startup reader: conflict detection + staleness compare + apply
# ---------------------------------------------------------------------------

# Where Voxtype stores downloaded whisper models. Used by the reader's
# missing-model check — if a sync bundle applies a model we don't have,
# the app surfaces a banner so the user can download it explicitly
# (never an auto-fetch).
DEFAULT_MODELS_DIR: Path = Path.home() / ".local" / "share" / "voxtype" / "models"

# Timestamp parsing tolerances. A bundle minted from another device may
# carry a `generated_at` in a slightly different ISO format (trailing Z,
# fractional seconds, different timezone offset). `datetime.fromisoformat`
# is lax enough for our inputs but needs the trailing `Z` swapped for
# `+00:00`.
_ISO_TRAILING_Z = "Z"


@dataclass
class SyncReconcileResult:
    """What happened during startup's sync reconciliation.

    Only one of `applied_from` / `conflict_files` / `skipped_reason` is
    typically meaningful at a time. The app shell reads these to drive
    banners:

      * `conflict_files` non-empty → persistent error banner, no apply.
      * `applied_from` set         → dismissible success banner.
      * `missing_model` set        → dismissible warning banner with a
                                     Download action.
      * `skipped_reason` alone     → silent; common single-device case.
    """
    applied_from: str | None = None
    applied_at: str | None = None
    conflict_files: list[Path] = field(default_factory=list)
    missing_model: str | None = None
    skipped_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def find_sync_conflict_files(sync_path: Path | None = None) -> list[Path]:
    """Return Syncthing conflict-copy files next to `sync_path`.

    Syncthing names conflicts as `<stem>.sync-conflict-<ts>-<hash>.<ext>`
    (see https://docs.syncthing.net/users/syncing.html#conflicting-changes).
    We glob for that pattern and return the list sorted by mtime so the
    oldest is first (matches chronological resolution UX).
    """
    path = sync_path or SYNC_PATH
    parent = path.parent
    if not parent.exists():
        return []
    pattern = f"{path.stem}.sync-conflict-*{path.suffix}"
    matches = list(parent.glob(pattern))
    matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
    return matches


def read_sync_bundle(sync_path: Path | None = None) -> Bundle | None:
    """Read and parse a bundle from disk, tolerantly.

    Returns None for:
      * File absent (common first-run case; no banner)
      * File unreadable (permissions, etc. — debug log, no banner)
      * Corrupt JSON / unknown format / schema too new (warning log;
        caller surfaces a banner)

    We never raise — startup must proceed even when sync.json is
    garbage, because a bad sync file shouldn't prevent the user from
    using the app locally.
    """
    path = sync_path or SYNC_PATH
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug("sync.json unreadable (%s); skipping reconcile", e)
        return None
    try:
        return from_json(raw.decode("utf-8"))
    except BundleError as e:
        logger.warning(
            "sync.json rejected (%s); skipping reconcile. File left in place.",
            e,
        )
        return None


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    normalized = ts.replace(_ISO_TRAILING_Z, "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Treat naive datetimes as UTC — we always write UTC, and
        # bundles from other timezones should be explicit.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _max_local_mtime(config_path: Path, sidecar_path: Path) -> datetime:
    """Latest mtime across the canonical files, as aware UTC datetime."""
    mtimes = []
    for p in (config_path, sidecar_path):
        try:
            mtimes.append(p.stat().st_mtime)
        except FileNotFoundError:
            continue
    if not mtimes:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(max(mtimes), tz=timezone.utc)


def _model_file_present(model_name: str, models_dir: Path) -> bool:
    """Best-effort check for whether Voxtype has a whisper model
    downloaded locally. Matches `ggml-<name>.bin`, the whisper.cpp
    convention that Voxtype's downloader uses."""
    if not models_dir.exists():
        return False
    return (models_dir / f"ggml-{model_name}.bin").exists()


def reconcile_sync_on_startup(
    doc: Any,
    sc: Sidecar,
    *,
    config_path: Path,
    sidecar_path: Path,
    sync_path: Path | None = None,
    models_dir: Path | None = None,
) -> SyncReconcileResult:
    """Startup hook: detect conflicts, apply stale local, flag missing models.

    Decision tree:

    1. **Conflict files present** → set `conflict_files`, return. No
       apply, no write. The user resolves manually via export/import.

    2. **No sync.json** → `skipped_reason = "no_file"`, return.

    3. **Corrupt / schema-too-new sync.json** → `skipped_reason` set,
       warning logged. Caller may or may not surface a banner.

    4. **Local mtime ≥ sync.generated_at** → local is equal or newer,
       `skipped_reason = "local_newer"`. Writer on next save will
       regenerate sync.json.

    5. **sync.generated_at > local mtime** → sync is newer. Verify the
       applied bundle would actually change state (content hash
       differs). If yes, merge into doc + sc AND write back to disk
       atomically so loaded state stays consistent with what's on disk.
       Set `applied_from` to the device label for the banner.

    6. Post-apply, check whisper.model against the local models dir;
       set `missing_model` if absent.

    Mutates `doc` and `sc` in place when apply fires. The caller's
    `loaded_dump` snapshot should be re-taken after this returns — it's
    AppState's responsibility to do that.

    Does NOT touch the daemon. A restart (if needed) is the user's
    explicit Ctrl+Shift+R.
    """
    result = SyncReconcileResult()
    sync_path = sync_path or SYNC_PATH
    models_dir = models_dir or DEFAULT_MODELS_DIR

    # --- 1. Conflict files short-circuit
    result.conflict_files = find_sync_conflict_files(sync_path)
    if result.conflict_files:
        result.skipped_reason = "conflict"
        return result

    # --- 2 & 3. Read + parse
    bundle = read_sync_bundle(sync_path)
    if bundle is None:
        # Distinguish "no file" from "corrupt/rejected" for banner logic.
        if sync_path.exists():
            result.skipped_reason = "corrupt"
            result.warnings.append(
                "sync.json could not be parsed; ignored. "
                "Next save will regenerate it."
            )
        else:
            result.skipped_reason = "no_file"
        return result

    # --- 4. Timestamp compare
    sync_dt = _parse_iso(bundle.generated_at)
    if sync_dt is None:
        result.skipped_reason = "corrupt"
        result.warnings.append(
            f"sync.json has unparseable generated_at "
            f"{bundle.generated_at!r}; skipping apply."
        )
        return result

    local_dt = _max_local_mtime(config_path, sidecar_path)
    if sync_dt <= local_dt:
        result.skipped_reason = "local_newer"
        return result

    # --- 5. Sync is newer. Content check: if the bundle's sync block
    # distills to the same content as current state, there's nothing
    # meaningful to apply (someone else's device happened to regenerate
    # a bundle with identical data). Skip to avoid a dirty-rewrite loop.
    current_sync = distill_sync(doc, sc.vocabulary, sc.replacements)
    if stable_hash(current_sync) == stable_hash(bundle.sync):
        result.skipped_reason = "identical"
        return result

    warnings = apply_bundle_to_state(bundle, doc, sc, include_local=False)
    result.warnings.extend(warnings)

    # Persist immediately so the loaded in-memory state stays in sync
    # with disk. This avoids the next save having to re-apply and
    # prevents a partial-state window if the app crashes.
    _persist_after_apply(doc, sc, config_path, sidecar_path)

    result.applied_from = bundle.generated_by_device or "(unknown device)"
    result.applied_at = bundle.generated_at

    # --- 6. Missing-model check on the applied settings
    model_name = _get(bundle.sync, "settings", "whisper", "model")
    if isinstance(model_name, str) and model_name:
        if not _model_file_present(model_name, models_dir):
            result.missing_model = model_name

    return result


def _persist_after_apply(
    doc: Any,
    sc: Sidecar,
    config_path: Path,
    sidecar_path: Path,
) -> None:
    """Atomically write doc + sc back to disk after a sync-apply.

    Uses `config.save_atomic` (NOT `config.safe_save`) to skip the
    voxtype-binary validation — voxtype may not be installed (test
    envs) or the sync may have introduced a config voxtype doesn't
    recognize yet, and we don't want startup to crash because of it.
    The next user-driven Ctrl+S goes through `safe_save` and gets
    validated there.
    """
    from . import config as _config  # avoid import cycle at module load
    from . import sidecar as _sidecar
    _config.save_atomic(doc, config_path)
    _sidecar.save_atomic(sc, sidecar_path)


def write_export_bundle(
    bundle: Bundle,
    target: Path,
) -> Path:
    """Atomically write a manual-export bundle to `target`.

    Returns the final resolved path (after `~` expansion). Creates the
    parent directory only if the user's chosen path descends into an
    existing tree — we never create a whole new directory structure
    from a user-typed path since that often indicates a typo (they
    meant `~/Documents/…` but typed `~/Docuements/…`). `mkdir(parents=False)`
    keeps a small directory creation in scope for the case where the
    user has `~/Downloads` but not `~/Downloads/voxtype-tui-exports`.

    Same atomic discipline as the sync writer: tempfile in the target
    directory, `os.replace`. A failure mid-write leaves the original
    file (if any) intact and cleans up the tempfile.
    """
    target = target.expanduser()
    parent = target.parent
    if not parent.exists():
        # Single missing leaf directory: create it. Deeper missing path
        # means the user probably typoed — surface that as an error.
        if parent.parent.exists():
            parent.mkdir(parents=False, exist_ok=True)
        else:
            raise FileNotFoundError(
                f"parent directory does not exist: {parent}"
            )
    _atomic_write_text(target, to_json(bundle))
    return target


def _existing_hash_matches(path: Path, new_hash: str) -> bool:
    """True when the file at `path` already embeds `new_hash` as its
    `local_sync_hash`. Any error reading / parsing the file returns False
    — better to rewrite a sane file over a corrupt one than to treat
    unreadable as "up to date"."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.debug("sync.json unreadable (%s); will overwrite", e)
        return False
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.debug("sync.json corrupt (%s); will overwrite", e)
        return False
    if not isinstance(parsed, dict):
        return False
    return parsed.get("local_sync_hash") == new_hash


def _atomic_write_text(path: Path, text: str) -> None:
    """Tempfile-in-same-dir + os.replace. Same pattern as
    `config.safe_save` / `sidecar.save_atomic` — if Syncthing is watching
    the directory, it sees either the old file or the new file, never a
    partially-written one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
