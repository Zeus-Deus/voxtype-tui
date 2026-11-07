"""Learned vocabulary: words the user taught us by correcting a dictation.

Learned entries live in their own file, ``$XDG_CONFIG_HOME/voxtype-tui/
learned.json``. They never touch the user's own vocabulary (metadata.json /
``[whisper] initial_prompt``) or dictionary (``[text] replacements``); the
only way a learned entry becomes a user entry is an explicit
:func:`promote`.

Learned entries act only in post-processing, through :func:`apply`, which
``voxtype-tui-postprocess`` calls after the dictionary engine and the
vocabulary corrector. That means no daemon restart, an immediate effect and
full reversibility (forget / reset / turning learning off).

Kinds:
  ``sounds_like``  "Omashi" -> "Omarchy". The heard form is not an English
                   word, so every exact occurrence of it is rewritten. The
                   written form is also a fuzzy corrector term, so close
                   variants ("Omachi") are caught too.
  ``spelling``     "Austin" -> "Austen". The heard form is an ordinary
                   word, so it is never blindly replaced; the written form
                   only becomes a fuzzy corrector term (the corrector's
                   English-wordlist guard stays in charge).
  ``replace``      only from an explicit choice in History > Fix: an exact
                   alias even when the heard form is an English word.

States: ``suggested`` (seen, not applied yet), ``active`` (applied),
``dormant`` (active but over the apply cap), ``rejected`` (tombstone, never
re-learned), ``promoted`` (now one of the user's own words; learning never
edits it again).

Everything here is stdlib only and Textual-free. The post-process path reads
a small compiled file (``learned.apply.json``) instead of the full store.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 1
KINDS = ("spelling", "sounds_like", "replace")
STATES = ("suggested", "active", "dormant", "rejected", "promoted")
SOURCES = ("fix", "atspi", "keys", "redictation")
PASSIVE_SOURCES = frozenset({"atspi", "keys", "redictation"})

# Guards (see docs/auto-learn.md).
MAX_SPAN_TOKENS = 3          # a correction replaces 1..3 tokens with 1..3 tokens
MAX_CHANGED_TOKENS = 3       # more than that in one sentence is a rewrite
PASSIVE_MAX_FRACTION = 0.30  # ... or more than 30 % of its words (passive)
FIX_MAX_FRACTION = 0.50      # explicit Fix is allowed a little more
MIN_SIMILARITY = 0.5
PASSIVE_MIN_COUNT = 2
PASSIVE_WINDOW_DAYS = 30
RECORDS_PER_ENTRY = 10
CONTEXT_CHARS = 60

# Apply caps. Exact aliases are a dict lookup and scale to thousands; the
# fuzzy corrector costs ~40 us per term per 60 words, so only the best
# ranked terms that could plausibly match the text are handed to it.
MAX_ALIASES = 2000
MAX_FUZZY_TERMS = 120        # active terms kept in the compiled file (ranked)
MAX_FUZZY_PER_TEXT = 12      # terms actually passed to the corrector per text
MAX_TEXT_CHARS = 50_000

PASSWORD_MANAGER_PATTERN = (
    r"1password|bitwarden|keepass|keepassxc|enpass|lastpass|proton\s*pass|"
    r"passwordsafe|gnome-keyring|seahorse|kwallet|pinentry"
)

_WORD = re.compile(r"[^\W_]+(?:['\u2019\-][^\W_]+)*", re.UNICODE)
_NON_ALNUM = re.compile(r"[^0-9a-z]")
_POSSESSIVE = re.compile(r"(['\u2019]s)$", re.IGNORECASE)


# ---------------------------------------------------------------- paths

def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "voxtype-tui"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "voxtype-tui"


def store_path() -> Path:
    return config_dir() / "learned.json"


def compiled_path() -> Path:
    """Small derived file read by post-process on every dictation."""
    return config_dir() / "learned.apply.json"


def features_path() -> Path:
    return config_dir() / "features.json"


def history_path() -> Path:
    return data_dir() / "history.jsonl"


# ---------------------------------------------------------------- time

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: object) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- IO

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p, 0o700)


def _atomic_write_json(path: Path, data: Any, *, indent: int | None = 2) -> None:
    import tempfile  # write path only: keep post-process imports lean

    _ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False,
                      separators=None if indent else (",", ":"))
            if indent:
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked():
    """Exclusive flock on ``learned.lock`` for read-modify-write cycles."""
    import fcntl

    d = config_dir()
    _ensure_dir(d)
    fd = os.open(str(d / "learned.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _empty_store() -> dict:
    return {"version": SCHEMA_VERSION, "entries": []}


def load() -> dict:
    """The whole store (``{"version", "entries", ...}``). Never raises;
    a missing or unreadable file is an empty store. Unknown keys are kept."""
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_store()
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return _empty_store()
    data["entries"] = [e for e in data["entries"] if _valid_entry(e)]
    data.setdefault("version", SCHEMA_VERSION)
    return data


def _valid_entry(e: object) -> bool:
    return (
        isinstance(e, dict)
        and isinstance(e.get("id"), str)
        and isinstance(e.get("written"), str) and e["written"].strip() != ""
        and e.get("kind") in KINDS
        and e.get("state") in STATES
        and isinstance(e.get("heard"), list)
    )


def _save(store: dict) -> None:
    """Write the store and the compiled apply file. Caller holds the lock."""
    _recompute_dormant(store)
    _atomic_write_json(store_path(), store)
    _atomic_write_json(compiled_path(), _compile(store), indent=None)


# ---------------------------------------------------------------- flags

def _features() -> dict:
    """features.json (owned by voxtype_tui.features). Read raw so the
    post-process path stays import-light; missing file = defaults."""
    try:
        data = json.loads(features_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def learning_flags() -> dict:
    lrn = _features().get("learning")
    lrn = lrn if isinstance(lrn, dict) else {}
    return {
        "enabled": lrn.get("enabled", True) is not False,
        "everywhere": lrn.get("everywhere", False) is True,
        "notify": lrn.get("notify", True) is not False,
        "keep_context": lrn.get("keep_context", False) is True,
    }


def is_enabled() -> bool:
    return learning_flags()["enabled"]


# ---------------------------------------------------------------- words

def squash(s: str) -> str:
    return _NON_ALNUM.sub("", s.lower())


def _skeleton(s: str) -> str:
    try:
        from .vocab_corrector import skeleton
        return skeleton(s)
    except Exception:
        return squash(s)


def _wordlist() -> frozenset[str] | None:
    try:
        from .vocab_corrector import wordlist
        return wordlist()
    except Exception:
        return None


def _is_english(phrase: str, words: frozenset[str] | None) -> bool:
    toks = [t.lower() for t in _WORD.findall(phrase)]
    if not toks or words is None:
        return False
    return all(t.strip("'\u2019") in words for t in toks)


def similarity(heard: str, written: str) -> float:
    a, b = squash(heard), squash(written)
    if not a or not b:
        return 0.0
    r = SequenceMatcher(None, a, b).ratio()
    if _skeleton(heard) == _skeleton(written):
        r = max(r, 0.9)
    return r


def is_password_manager(app_class: object) -> bool:
    return isinstance(app_class, str) and bool(
        re.search(PASSWORD_MANAGER_PATTERN, app_class, re.IGNORECASE))


# ---------------------------------------------------------------- diff

def _tokens(text: str) -> list[tuple[str, int, int]]:
    """Whitespace tokens with their punctuation stripped: (core, start, end)."""
    out = []
    for m in re.finditer(r"\S+", text):
        raw = m.group(0)
        lead = len(raw) - len(raw.lstrip("\"'([{\u201c\u2018<*"))
        core = raw.strip("\"'()[]{}<>,.;:!?\u201c\u201d\u2018\u2019*")
        if not core:
            continue
        start = m.start() + raw.find(core, lead) if core in raw else m.start()
        out.append((core, start, start + len(core)))
    return out


def diff_candidates(original: str, edited: str, *, passive: bool) -> tuple[list[dict], str | None]:
    """Word-level replace ops between ``original`` and ``edited``.

    Returns ``(pairs, reason)``: ``pairs`` are ``{"heard", "written",
    "context"}`` for each 1..3 -> 1..3 token replacement; ``reason`` is set
    when the whole edit is rejected (a rewrite)."""
    a = _tokens(original)
    b = _tokens(edited)
    if not a or not b:
        return [], "empty text"
    sm = SequenceMatcher(None, [t[0] for t in a], [t[0] for t in b], autojunk=False)
    ops = sm.get_opcodes()
    changed = 0
    pairs = []
    for i, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            continue
        if tag == "insert" and i1 == len(a):
            continue  # the user kept typing after the dictation
        if tag == "replace" and i2 == len(a) and (j2 - j1) > (i2 - i1):
            j2 = j1 + (i2 - i1)  # a fix of the last words, then more typing
        changed += max(i2 - i1, j2 - j1)
        if tag != "replace":
            continue
        if (i2 - i1) > MAX_SPAN_TOKENS or (j2 - j1) > MAX_SPAN_TOKENS:
            continue
        heard = original[a[i1][1]:a[i2 - 1][2]]
        written = edited[b[j1][1]:b[j2 - 1][2]]
        ctx_a = max(0, a[i1][1] - 25)
        pairs.append({
            "heard": " ".join(heard.split()),
            "written": " ".join(written.split()),
            "context": original[ctx_a:ctx_a + CONTEXT_CHARS],
        })
    n = len(a)
    frac = PASSIVE_MAX_FRACTION if passive else FIX_MAX_FRACTION
    if changed > MAX_CHANGED_TOKENS or changed > max(1, int(n * frac + 1e-9)):
        return [], "too many words changed (a rewrite, not a correction)"
    return pairs, None


def classify(heard: str, written: str, *, user_vocab: Iterable[str] = (),
             user_triggers: Iterable[str] = (), kind: str | None = None,
             words: frozenset[str] | None = None,
             passive: bool = False) -> tuple[str | None, str | None]:
    """Decide the learned kind for one pair, or why it is skipped.

    Returns ``(kind, None)`` or ``(None, reason)``."""
    if words is None:
        words = _wordlist()
    h, w = heard.strip(), written.strip()
    if not h or not w or h == w:
        return None, "no change"
    if not squash(w):
        return None, "only punctuation changed"
    letters = sum(c.isalpha() for c in w)
    if letters <= 2:
        return None, "too short"
    if any(c.isdigit() for c in w) and not any(c.isalpha() for c in w):
        return None, "a number"
    uv = {v.strip().lower() for v in user_vocab if isinstance(v, str)}
    ut = {t.strip().lower() for t in user_triggers if isinstance(t, str)}
    if h.lower() in ut or h.lower() in uv:
        return None, "already covered by your own words"
    if kind == "replace":
        return "replace", None
    if _is_english(w, words) and (w == w.lower() or squash(h) == squash(w)):
        # "their" -> "there", "Hello" -> "hello", "every day" -> "everyday"
        return None, "an ordinary word (grammar or wording, not vocabulary)"
    if squash(h) == squash(w):
        # same letters: casing or spacing of a name ("code mux" -> "Codemux")
        if w.lower() in uv:
            return None, "already in your vocabulary"
        return (kind if kind in ("spelling", "sounds_like") else "spelling"), None
    if similarity(h, w) < MIN_SIMILARITY:
        return None, "the words are too different (a different word, not a mishearing)"
    if kind in ("spelling", "sounds_like"):
        k = kind
    elif words is None and passive:
        # Without a wordlist we cannot tell "Austin" (a word) from "Omashi"
        # (a mishearing): a passive capture never creates an exact alias then.
        k = "spelling"
    else:
        k = "spelling" if _is_english(h, words) else "sounds_like"
    if k == "spelling" and w.lower() in uv:
        return None, "already in your vocabulary"
    return k, None


# ---------------------------------------------------------------- records

def find_record(record_id: str) -> dict | None:
    """Look a dictation record up by id (history.jsonl, newest first)."""
    try:
        import importlib
        history = importlib.import_module("voxtype_tui.history")  # insights, optional
        get = getattr(history, "get", None)
        if callable(get):
            rec = get(record_id)
            if isinstance(rec, dict):
                return rec
    except Exception:
        pass
    try:
        lines = history_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if record_id not in line:
            continue
        with contextlib.suppress(ValueError):
            rec = json.loads(line)
            if isinstance(rec, dict) and rec.get("id") == record_id:
                return rec
    return None


def _user_lists() -> tuple[list[str], list[str]]:
    """(user vocabulary phrases, user dictionary triggers) from the sidecar."""
    try:
        from .cli_postprocess import SIDECAR_PATH
        path = SIDECAR_PATH
    except Exception:
        path = Path.home() / ".config" / "voxtype-tui" / "metadata.json"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    vocab = []
    for v in data.get("vocabulary", []) if isinstance(data, dict) else []:
        p = v.get("phrase") if isinstance(v, dict) else v
        if isinstance(p, str):
            vocab.append(p)
    trig = []
    for r in data.get("replacements", []) if isinstance(data, dict) else []:
        if isinstance(r, dict):
            t = r.get("from_text") or r.get("from")
            if isinstance(t, str):
                trig.append(t)
    return vocab, trig


# ---------------------------------------------------------------- submit

_KIND_RANK = {"spelling": 0, "sounds_like": 1, "replace": 2}


def _new_id() -> str:
    import secrets

    return "l_" + secrets.token_hex(5)


def _find_entry(store: dict, written: str) -> dict | None:
    for e in store["entries"]:
        if e["written"] == written:
            return e
    return None


def _unambiguous(pair_count: int, heard: str, written: str, kind: str) -> bool:
    """A passive capture activates on the first sighting only when it is
    clearly an ASR fix: one single-token replacement, of a heard token that
    is not an English word, into a close-sounding written form."""
    return (
        pair_count == 1
        and kind == "sounds_like"
        and len(heard.split()) == 1 and len(written.split()) == 1
        and similarity(heard, written) >= 0.6
    )


def submit_correction(record: dict | str, edited_text: str, source: str = "fix", *,
                      kind: str | None = None, app: str | None = None,
                      now: datetime | None = None) -> dict:
    """Learn from a corrected dictation.

    ``record`` is a dictation record (``{"id", "final", "raw", "app"}``) or
    its id. ``edited_text`` is the text as the user corrected it. Returns
    ``{"ok", "candidates":[{heard, written, kind}], "applied":[entry],
    "skipped":[{heard, written, reason}], "reason"?}``."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if kind == "replace" and source != "fix":
        raise ValueError("'replace' is only learned from an explicit fix")
    out: dict[str, Any] = {"ok": True, "candidates": [], "applied": [], "skipped": []}
    if not learning_flags()["enabled"]:
        out["reason"] = "learning is off"
        return out
    rec = find_record(record) if isinstance(record, str) else record
    if not isinstance(rec, dict):
        return {**out, "ok": False, "reason": "dictation not found"}
    original = rec.get("final") if isinstance(rec.get("final"), str) else rec.get("raw")
    if not isinstance(original, str) or not isinstance(edited_text, str):
        return {**out, "ok": False, "reason": "dictation has no text"}
    rec_app = rec.get("app")
    app_class = app or (rec_app.get("class") if isinstance(rec_app, dict) else None)
    if source in PASSIVE_SOURCES and is_password_manager(app_class):
        out["reason"] = "password manager windows are never watched"
        return out
    passive = source in PASSIVE_SOURCES
    pairs, reason = diff_candidates(original, edited_text, passive=passive)
    if reason:
        out["reason"] = reason
        return out
    if not pairs:
        out["reason"] = "no word was corrected"
        return out
    words = _wordlist()
    uv, ut = _user_lists()
    now = now or _now()
    keep_ctx = learning_flags()["keep_context"]
    rec_id = rec.get("id") if isinstance(rec.get("id"), str) else None
    with _locked():
        store = load()
        changed = False
        for p in pairs:
            k, why = classify(p["heard"], p["written"], user_vocab=uv, user_triggers=ut,
                              kind=kind, words=words, passive=passive)
            if k is None:
                out["skipped"].append({"heard": p["heard"], "written": p["written"], "reason": why})
                continue
            out["candidates"].append({"heard": p["heard"], "written": p["written"], "kind": k})
            e = _find_entry(store, p["written"])
            if e is not None and e["state"] in ("rejected", "promoted"):
                out["skipped"].append({"heard": p["heard"], "written": p["written"],
                                       "reason": "you rejected this before" if e["state"] == "rejected"
                                       else "already one of your own words"})
                continue
            if e is None:
                e = {
                    "id": _new_id(), "kind": k, "written": p["written"], "heard": [],
                    "state": "suggested", "source": source, "sources": {},
                    "app": app_class if isinstance(app_class, str) else None,
                    "count": 0, "hits": 0, "first_seen": _iso(now), "last_seen": _iso(now),
                    "decided_at": None, "records": [], "seen": [], "aliases": [],
                }
                store["entries"].append(e)
            if _KIND_RANK[k] > _KIND_RANK.get(e["kind"], 0):
                e["kind"] = k
            if p["heard"].lower() not in {h.lower() for h in e["heard"]}:
                e["heard"].append(p["heard"])
            # Exact aliases are decided per heard form: "Auston" may be an
            # alias of "Austen" while the English "Austin" never is.
            aliases = e.setdefault("aliases", [])
            if k in ("sounds_like", "replace") and \
                    p["heard"].lower() not in {h.lower() for h in aliases}:
                aliases.append(p["heard"])
            duplicate = rec_id is not None and rec_id in e.get("records", [])
            if not duplicate:
                e["count"] = int(e.get("count", 0)) + 1
                e.setdefault("seen", []).append(_iso(now))
                e["seen"] = e["seen"][-RECORDS_PER_ENTRY:]
                if rec_id:
                    e.setdefault("records", []).append(rec_id)
                    e["records"] = e["records"][-RECORDS_PER_ENTRY:]
            e.setdefault("sources", {})[source] = int(e["sources"].get(source, 0)) + (0 if duplicate else 1)
            e["last_seen"] = _iso(now)
            if keep_ctx:
                e["context"] = p["context"]
            if e["state"] in ("suggested", "dormant"):
                if not passive:
                    e["state"] = "active"
                elif _unambiguous(len(pairs), p["heard"], p["written"], e["kind"]):
                    e["state"] = "active"
                elif _recent_count(e, now) >= PASSIVE_MIN_COUNT:
                    e["state"] = "active"
            out["applied"].append(e)
            changed = True
        if changed:
            _save(store)
    out["applied"] = [_public(e) for e in out["applied"]]
    return out


def _recent_count(e: dict, now: datetime) -> int:
    cutoff = now - timedelta(days=PASSIVE_WINDOW_DAYS)
    n = 0
    for s in e.get("seen", []):
        dt = _parse_iso(s)
        if dt is not None and dt >= cutoff:
            n += 1
    return n


# ---------------------------------------------------------------- decisions

def _mutate(entry_id: str, fn: Callable[[dict, dict], None]) -> dict | None:
    with _locked():
        store = load()
        for e in store["entries"]:
            if e["id"] == entry_id:
                fn(store, e)
                _save(store)
                return _public(e)
    return None


def accept(entry_id: str) -> dict | None:
    def f(_s, e):
        if e["state"] in ("suggested", "dormant", "rejected"):
            e["state"] = "active"
            e["decided_at"] = _iso(_now())
    return _mutate(entry_id, f)


def reject(entry_id: str) -> dict | None:
    """Tombstone: stop applying it and never learn it again."""
    def f(_s, e):
        if e["state"] != "promoted":
            e["state"] = "rejected"
            e["decided_at"] = _iso(_now())
    return _mutate(entry_id, f)


def forget(entry_id: str) -> bool:
    """Delete one entry (it may be learned again later)."""
    with _locked():
        store = load()
        n = len(store["entries"])
        store["entries"] = [e for e in store["entries"] if e["id"] != entry_id]
        if len(store["entries"]) == n:
            return False
        _save(store)
        return True


def undo(entry_id: str, record_id: str | None = None) -> bool:
    """Undo the last learning step for an entry (notification Undo): a
    first-time entry is forgotten, otherwise its count goes back by one."""
    with _locked():
        store = load()
        for e in store["entries"]:
            if e["id"] != entry_id:
                continue
            if int(e.get("count", 0)) <= 1:
                store["entries"].remove(e)
            else:
                e["count"] = int(e["count"]) - 1
                if e.get("seen"):
                    e["seen"].pop()
                if record_id and record_id in e.get("records", []):
                    e["records"].remove(record_id)
                if e["state"] == "active" and e.get("decided_at") is None:
                    e["state"] = "suggested"
            _save(store)
            return True
    return False


def reset() -> int:
    """Delete every learned entry (tombstones too). Returns how many."""
    with _locked():
        store = load()
        n = len(store["entries"])
        store["entries"] = []
        _save(store)
        return n


def promote(entry_id: str, *, add_to_user: Callable[[dict], dict] | None = None,
            config_path: Path | None = None, sidecar_path: Path | None = None) -> dict:
    """Make a learned entry one of the user's own words.

    spelling -> vocabulary phrase; sounds_like / replace -> one dictionary
    Replacement rule per heard form. ``add_to_user(entry)`` lets the caller
    (the plugin bridge) do the user-side write with its own state loader;
    by default this uses ``AppState`` like the TUI. Returns
    ``{"ok", "entry", "added": {"vocab":[...], "rules":[[from, to], ...]}}``."""
    store = load()
    entry = next((e for e in store["entries"] if e["id"] == entry_id), None)
    if entry is None:
        return {"ok": False, "error": "no such learned entry"}
    if entry["state"] == "promoted":
        return {"ok": True, "entry": _public(entry), "added": {"vocab": [], "rules": []}}
    added = (add_to_user or (lambda e: _add_to_user_default(e, config_path, sidecar_path)))(entry)

    def f(_s, e):
        e["state"] = "promoted"
        e["decided_at"] = _iso(_now())
    pub = _mutate(entry_id, f)
    return {"ok": True, "entry": pub, "added": added}


def user_additions(entry: dict) -> dict:
    """What promoting ``entry`` adds to the user's own lists."""
    aliases = entry.get("aliases") if isinstance(entry.get("aliases"), list) else entry["heard"]
    if entry["kind"] == "spelling" or not aliases:
        return {"vocab": [entry["written"]], "rules": []}
    return {"vocab": [], "rules": [[h, entry["written"]] for h in aliases]}


def _add_to_user_default(entry: dict, config_path: Path | None, sidecar_path: Path | None) -> dict:
    from . import config, sidecar, state

    st = state.AppState.load(config_path or config.CONFIG_PATH, sidecar_path or sidecar.SIDECAR_PATH)
    add = user_additions(entry)
    done = {"vocab": [], "rules": []}
    for phrase in add["vocab"]:
        if st.add_vocab(phrase):
            done["vocab"].append(phrase)
    existing = config.get_replacements(st.doc)
    for frm, to in add["rules"]:
        if frm not in existing:
            st.upsert_replacement(frm, to, sidecar.DEFAULT_CATEGORY)
            done["rules"].append([frm, to])
    if st.dirty:
        st.save()
    return done


# ---------------------------------------------------------------- ranking

def score(e: dict, now: datetime | None = None) -> float:
    now = now or _now()
    last = _parse_iso(e.get("last_hit") or e.get("last_seen")) or now
    age_days = max(0.0, (now - last).total_seconds() / 86400)
    recency = 2.0 * 0.5 ** (age_days / 14.0)
    return 3.0 * int(e.get("count", 0)) + int(e.get("hits", 0)) + recency


def _recompute_dormant(store: dict) -> None:
    """Active entries past the apply caps go dormant; dormant ones come back
    when there is room."""
    now = _now()
    live = [e for e in store["entries"] if e["state"] in ("active", "dormant")]
    live.sort(key=lambda e: score(e, now), reverse=True)
    n_alias = n_terms = 0
    for e in live:
        alias = e["kind"] in ("sounds_like", "replace")
        fits = (n_alias < MAX_ALIASES) if alias else (n_terms < MAX_FUZZY_TERMS)
        if alias:
            n_alias += 1
        else:
            n_terms += 1
        e["state"] = "active" if fits else "dormant"


def _compile(store: dict) -> dict:
    """The post-process view: exact aliases + ranked fuzzy terms."""
    now = _now()
    aliases: dict[str, list[str]] = {}
    terms: list[list[str]] = []
    max_n = 1
    active = [e for e in store["entries"] if e["state"] == "active"]
    active.sort(key=lambda e: score(e, now), reverse=True)
    for e in active:
        if e["kind"] in ("sounds_like", "replace"):
            for h in (e["aliases"] if isinstance(e.get("aliases"), list) else e["heard"]):
                key = " ".join(t.lower() for t in _WORD.findall(h))
                if key and key not in aliases and key != e["written"].lower():
                    aliases[key] = [e["written"], e["id"]]
                    max_n = max(max_n, len(key.split()))
        if e["kind"] in ("sounds_like", "spelling") and len(terms) < MAX_FUZZY_TERMS:
            if e["written"].isascii() and len(squash(e["written"])) >= 4:
                w = e["written"]
                terms.append([w, e["id"], squash(w), len(w.split()), _skeleton(w)])
    return {"version": 1, "aliases": aliases, "max_n": max_n, "terms": terms}


def _public(e: dict) -> dict:
    out = {k: v for k, v in e.items() if k not in ("seen",)}
    out["score"] = round(score(e), 2)
    return out


def list_entries() -> dict:
    """For the panel: entries ranked, plus counts per state."""
    store = load()
    now = _now()
    entries = sorted(store["entries"], key=lambda e: score(e, now), reverse=True)
    counts = {s: 0 for s in ("active", "suggested", "dormant", "rejected", "promoted")}
    for e in entries:
        counts[e["state"]] = counts.get(e["state"], 0) + 1
    flags = learning_flags()
    return {"enabled": flags["enabled"], "everywhere": flags["everywhere"],
            "entries": [_public(e) for e in entries], "counts": counts}


def record_hits(hit_ids: Iterable[str]) -> None:
    """Bump ``hits`` for entries :func:`apply` used. Call it OFF the
    critical path (post-output hook / watcher), never from post_process."""
    ids = [i for i in hit_ids if isinstance(i, str)]
    if not ids:
        return
    with _locked():
        store = load()
        now = _iso(_now())
        hit = False
        for e in store["entries"]:
            c = ids.count(e["id"])
            if c:
                e["hits"] = int(e.get("hits", 0)) + c
                e["last_hit"] = now
                hit = True
        if hit:
            _save(store)


# ---------------------------------------------------------------- apply

def _load_compiled() -> dict | None:
    try:
        data = json.loads(compiled_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _overlaps(a: int, b: int, spans: list[tuple[int, int]]) -> bool:
    return any(a < e and s < b for s, e in spans)


def _phrase_spans(text: str, phrases: Iterable[str]) -> list[tuple[int, int]]:
    """Occurrences (case-insensitive, word-bounded) of the user's own output
    strings: their dictionary replacements and vocabulary. Learned entries
    must never rewrite inside them. Offsets are taken on ``text`` itself
    (lowercasing can change string length for some Unicode letters)."""
    spans = []
    low = text.lower()
    for p in phrases:
        if not isinstance(p, str) or not p.strip():
            continue
        p = p.strip()
        # cheap C-level pre-filter; only phrases that occur pay for a regex
        if p.lower() not in low and p.casefold() not in text.casefold():
            continue
        for m in re.finditer(re.escape(p), text, re.IGNORECASE):
            i, j = m.start(), m.end()
            left = i == 0 or not text[i - 1].isalnum()
            right = j >= len(text) or not text[j].isalnum()
            if left and right:
                spans.append((i, j))
    return spans


# A whitespace-delimited chunk that is a URL, path, e-mail or query string is
# protected as a whole ("https://x.org/s?q=hello+omashi", "~/omashi/x").
_URLISH = re.compile(
    r"\S*(?:://|www\.|\w[/\\@]\w|[?&]\w+=)\S*"   # URL, a/b path, e-mail, query
    r"|(?<!\S)(?:~|\.{1,2})?/\S+"                  # /abs, ~/x, ./x
)


def _url_like(text: str, a: int, b: int) -> bool:
    """Part of a URL, path, file name or identifier (same rule as the
    vocabulary corrector): never rewrite those."""
    n = len(text)
    if a > 0 and text[a - 1] in "./\\_@:#~=":
        return True
    if b < n and text[b] in "_@=" :
        return True
    if b < n - 1 and text[b] in "./\\:" and (text[b + 1].isalnum() or text[b + 1] in "/\\"):
        return True
    return False


def apply(text: str, protected_spans: Iterable[tuple[int, int]] = (), *,
          user_phrases: Iterable[str] = ()) -> tuple[str, list[str]]:
    """Rewrite ``text`` with the active learned entries.

    Runs after the user's dictionary and vocabulary corrector. It never
    rewrites inside ``protected_spans`` (character ranges of ``text`` the
    dictionary produced) or inside occurrences of ``user_phrases`` (the
    user's dictionary outputs and vocabulary). Returns ``(text, hit_ids)``.
    Never raises; with learning off, nothing learned, or any error it returns
    ``(text, [])``."""
    original = text
    try:
        if not text or not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
            return text, []
        comp = _load_compiled()
        if not comp or not (comp.get("aliases") or comp.get("terms")):
            return text, []
        if not learning_flags()["enabled"]:
            return text, []
        prot = [(int(a), int(b)) for a, b in protected_spans]
        prot += _phrase_spans(text, user_phrases)
        prot += [m.span() for m in _URLISH.finditer(text)]
        hits: list[str] = []
        text, prot = _apply_aliases(text, comp, prot, hits)
        text = _apply_terms(text, comp, prot, hits)
        return text, hits
    except Exception:
        return original, []


def _apply_aliases(text: str, comp: dict, prot: list[tuple[int, int]],
                   hits: list[str]) -> tuple[str, list[tuple[int, int]]]:
    aliases = comp.get("aliases") or {}
    if not aliases:
        return text, prot
    max_n = max(1, min(MAX_SPAN_TOKENS, int(comp.get("max_n", 1))))
    toks = list(_WORD.finditer(text))
    edits = []
    i = 0
    while i < len(toks):
        done = False
        for n in range(min(max_n, len(toks) - i), 0, -1):
            group = toks[i:i + n]
            if n > 1 and not all(text[group[k].end():group[k + 1].start()].isspace()
                                 for k in range(n - 1)):
                continue
            words = [g.group(0) for g in group]
            suffix = ""
            m = _POSSESSIVE.search(words[-1])
            key = " ".join(w.lower() for w in words)
            hit = aliases.get(key)
            if hit is None and m:
                words[-1] = words[-1][:m.start()]
                key = " ".join(w.lower() for w in words)
                hit = aliases.get(key)
                suffix = m.group(1) if hit else ""
            if hit is None:
                continue
            a = group[0].start()
            b = group[-1].end() - len(suffix)
            if _overlaps(a, b, prot) or _url_like(text, a, group[-1].end()):
                continue
            edits.append((a, b, hit[0]))
            hits.append(hit[1])
            i += n
            done = True
            break
        if not done:
            i += 1
    if not edits:
        return text, prot
    # Rebuild text; shift protected spans and protect what we wrote.
    out = []
    pos = 0
    shift_points = []
    new_prot = []
    delta = 0
    for a, b, w in edits:
        out.append(text[pos:a])
        new_start = a + delta
        out.append(w)
        new_prot.append((new_start, new_start + len(w)))
        shift_points.append((b, len(w) - (b - a)))
        delta += len(w) - (b - a)
        pos = b
    out.append(text[pos:])

    def shift(x: int) -> int:
        return x + sum(d for at, d in shift_points if at <= x)
    moved = [(shift(s), shift(e)) for s, e in prot]
    return "".join(out), moved + new_prot


_WORDS_FILE = Path("/usr/share/cracklib/cracklib-small")  # byte-sorted, lowercase


def _english_word(w: str) -> bool | None:
    """Is ``w`` an ordinary English word? ``None`` when no wordlist is
    available. Uses the corrector's wordlist when this process already
    loaded it, else a binary search in the sorted cracklib file (no ~9 ms
    load on the post-process path)."""
    w = w.lower().strip("'\u2019")
    try:
        from . import vocab_corrector as vc
        if vc._words is not vc._UNLOADED:
            return None if vc._words is None else w in vc._words  # type: ignore[operator]
        if not _WORDS_FILE.is_file():
            words = _wordlist()
            return None if words is None else w in words
        import mmap
        with open(_WORDS_FILE, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            key = w.encode()
            lo, hi = 0, len(mm)
            while lo < hi:  # find the first line >= key
                mid = (lo + hi) // 2
                ls = mm.rfind(b"\n", 0, mid) + 1
                le = mm.find(b"\n", ls)
                le = len(mm) if le < 0 else le
                if mm[ls:le] < key:
                    lo = le + 1
                else:
                    hi = ls
            le = mm.find(b"\n", lo)
            return mm[lo:(len(mm) if le < 0 else le)] == key
    except Exception:
        return None


MAX_FUZZY_PAIRS = 150  # bound on similarity computations per dictation


def _apply_terms(text: str, comp: dict, prot: list[tuple[int, int]], hits: list[str]) -> str:
    """Fuzzy stage for learned spellings: the vocabulary corrector's rules
    (same thresholds, plural/hyphen/English guards, ASCII only), for spans of
    the term's own width or one less (multi-word terms) / 1-2 tokens
    (single words), outside protected text."""
    terms = comp.get("terms") or []
    if not terms:
        return text
    toks = list(_WORD.finditer(text))
    if not toks:
        return text
    clean = []
    widths_needed = {1, 2}
    for t in terms[:MAX_FUZZY_TERMS]:
        if not isinstance(t, (list, tuple)) or not t or not isinstance(t[0], str):
            continue
        written, eid = t[0], t[1] if len(t) > 1 else None
        if len(t) >= 5:
            tq, n, tk = t[2], int(t[3]), t[4]
        else:  # older compiled file
            tq, n, tk = squash(written), len(written.split()), None
        if not tq:
            continue
        clean.append((written, eid, tq, n, tk))
        if n > 1:
            widths_needed.update({n - 1, n})
    if not clean:
        return text
    max_w = min(MAX_SPAN_TOKENS, max(widths_needed))
    # spans bucketed by (first letter, squashed length)
    buckets: dict[tuple[str, int], list[tuple]] = {}
    for i in range(len(toks)):
        for w in range(1, max_w + 1):
            if i + w > len(toks):
                break
            g = toks[i:i + w]
            if not all(text[g[k].end():g[k + 1].start()].isspace() for k in range(w - 1)):
                break
            a, b = g[0].start(), g[-1].end()
            span = text[a:b]
            if not span.isascii():
                break  # Unicode letters/marks: the ASCII matcher keeps out
            nb = text[max(0, a - 1):a] + text[b:b + 1]
            if any(not c.isascii() and (c.isalnum() or unicodedata.category(c).startswith("M"))
                   for c in nb):
                continue
            if _overlaps(a, b, prot) or _url_like(text, a, b):
                continue
            words = [x.group(0) for x in g]
            if w >= 2 and (words[0].lower() in _STOP or words[-1].lower() in _STOP):
                continue
            sq = squash(span)
            if sq:
                buckets.setdefault((sq[0], len(sq)), []).append((a, b, sq, w, words))
    if not buckets:
        return text
    cands = []
    budget = MAX_FUZZY_PAIRS
    skel_cache: dict[str, str] = {}
    picked = 0
    for written, eid, tq, n, tk in clean:
        widths = (n - 1, n) if n > 1 else (1, 2)
        tol = max(2, len(tq) // 3)
        f0 = tq[0]
        sm = None
        matched = False
        lens = sorted({len(tq), *range(max(5, len(tq) - tol), len(tq) + tol + 1)})
        for ln in lens:
            for a, b, sq, w, words in buckets.get((f0, ln), ()):
                if w not in widths:
                    continue
                if sq == tq:
                    # same letters: fix casing/spacing, and reserve an already
                    # correct span so a partial fuzzy match can't overwrite it
                    cands.append((3 if text[a:b] == written else 2, w, 2.0, a, b, written, eid))
                    matched = True
                    continue
                if len(sq) < 5 or sq in (tq + "s", tq + "es"):
                    continue
                if w == 1 and "-" in words[0]:
                    continue
                if budget <= 0:
                    continue
                budget -= 1
                if sm is None:
                    sm = SequenceMatcher(None, "", tq)  # seq2 analysis is cached
                sm.set_seq1(sq)
                if sm.real_quick_ratio() < 0.6 or sm.quick_ratio() < 0.6:
                    continue
                r1 = sm.ratio()
                need = 0.88 if len(tq) <= 6 else 0.80
                if w > n:
                    need = max(need, 0.85)
                score = r1
                if r1 < need:
                    tk = tk or _skeleton(written)
                    sk = skel_cache.get(text[a:b])
                    if sk is None:
                        sk = skel_cache[text[a:b]] = _skeleton(text[a:b])
                    score = max(r1, 0.95 * SequenceMatcher(None, sk, tk).ratio())
                if score < need:
                    continue
                english = [_english_word(x) for x in words]
                if any(e is None for e in english):
                    continue  # no wordlist: only same-letter fixes are safe
                if all(english) and not (w >= 2 and n >= 2):
                    continue  # real English ("cloud", "preset") is never fuzzed
                cands.append((1, w, score, a, b, written, eid))
                matched = True
        # exact (same-letter) spans are length-equal, so the loop above saw them
        if matched:
            picked += 1
            if picked >= MAX_FUZZY_PER_TEXT:
                break
    if not cands:
        return text
    taken: list[tuple[int, int]] = []
    edits = []
    for tier, w, score, a, b, written, eid in sorted(cands, key=lambda c: c[:3], reverse=True):
        if any(a < tb and ta < b for ta, tb in taken):
            continue
        taken.append((a, b))
        if text[a:b] != written:
            edits.append((a, b, written, eid))
    for a, b, written, eid in sorted(edits, reverse=True):
        text = text[:a] + written + text[b:]
        hits.append(eid)
    return text


_STOP = frozenset({
    "a", "an", "the", "and", "but", "or", "in", "on", "of", "to", "for",
    "with", "is", "it", "my", "as", "at", "by", "be", "then", "that",
    "this", "so", "if", "what", "you", "i", "we", "can",
})


# ---------------------------------------------------------------- notify

def notification_text(entries: list[dict]) -> tuple[str, str]:
    names = [e["written"] for e in entries]
    if len(names) == 1:
        body = f'Added "{names[0]}" from your correction.'
    else:
        body = "Added " + ", ".join(f'"{n}"' for n in names) + " from your correction."
    return "Vocabulary learned", body


def notify_learned(entries: list[dict], *, record_id: str | None = None,
                   wait_for_undo_s: float = 0) -> bool:
    """Desktop notification for newly active entries. With
    ``wait_for_undo_s`` > 0 it offers an Undo action (mako supports
    actions) and waits that long for it; returns True if Undo was chosen.
    Never raises. Respects ``learning.notify``."""
    import shutil
    import subprocess

    try:
        if not entries or not learning_flags()["notify"]:
            return False
        exe = shutil.which("notify-send")
        if not exe:
            return False
        title, body = notification_text(entries)
        argv = [exe, "-a", "Voxtype", "-u", "low", "-i", "accessories-dictionary"]
        if wait_for_undo_s > 0:
            # mako runs the "default" action on a click, so a click = Undo.
            argv += ["--action=default=Undo"]
            body += " Click to undo."
        argv += [title, body]
        if wait_for_undo_s <= 0:
            subprocess.run(argv, timeout=5, check=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return False
        try:
            p = subprocess.run(argv, timeout=wait_for_undo_s, check=False,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            return False
        if p.stdout.strip() in ("default", "undo"):
            for e in entries:
                undo(e["id"], record_id)
            return True
        return False
    except Exception:
        return False


def _bench(n_entries: int = 500, words: int = 60) -> dict:  # pragma: no cover
    """Timing helper used by tests/docs: p50/p95 of apply() in ms."""
    import random
    random.seed(7)
    with _locked():
        store = _empty_store()
        for i in range(n_entries):
            w = "Zq" + "".join(random.choice("aeioutrnslkmp") for _ in range(6)) + str(i % 10)
            w = w.rstrip("0123456789") + "x" * (i % 3)
            store["entries"].append({
                "id": f"l_{i:05x}", "kind": "sounds_like" if i % 2 else "spelling",
                "written": w, "heard": [w.lower() + "h"], "aliases": [w.lower() + "h"], "state": "active",
                "source": "fix", "sources": {"fix": 1}, "count": 1, "hits": 0,
                "first_seen": _iso(_now()), "last_seen": _iso(_now()), "decided_at": None,
            })
        store["entries"].append({
            "id": "l_omarchy", "kind": "sounds_like", "written": "Omarchy",
            "heard": ["Omashi"], "state": "active", "source": "fix", "sources": {"fix": 1},
            "count": 1, "hits": 0, "first_seen": _iso(_now()), "last_seen": _iso(_now()),
            "decided_at": None,
        })
        _save(store)
    base = ("today we are going to build something great with computers and the "
            "default dictation option on Omashi is really quite good for writing ")
    text = " ".join((base * 4).split()[:words])
    ts = []
    for _ in range(200):
        t = time.perf_counter()
        apply(text)
        ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    return {"p50_ms": ts[len(ts) // 2], "p95_ms": ts[int(len(ts) * 0.95)], "max_ms": ts[-1]}
