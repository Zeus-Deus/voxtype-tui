"""Always-on vocabulary corrector, run by `voxtype-tui-postprocess`.

Speech engines often mishear proper nouns and jargon even when the
vocabulary is passed to them as a prompt ("clawed code" for Claude Code,
"hyperland" for Hyprland, "code mux" for Codemux), and engines without a
prompt (parakeet, moonshine, …) never see the vocabulary at all. This
module repairs those spans after transcription, on every engine.

For each vocabulary phrase it looks at spans of 1..n+1 words, compares a
squashed lowercase form (letters and digits only) with a similarity ratio,
plus a rough consonant-skeleton key that catches sound-alikes, and
rewrites the span to the canonical spelling. It is conservative on
purpose:

- spans never cross punctuation;
- non-ASCII vocabulary and spans touching Unicode letters, digits or
  combining marks are left alone (the matcher uses ASCII tokens);
- URLs, paths and file names are left alone (``app.codemux.org``,
  ``.codex``, ``claude.md``);
- plural forms of a term are kept as the user said them;
- hyphenated identifiers only match exactly, never fuzzily;
- terms of 3 or fewer letters (``MCP``) are never touched;
- a span made only of ordinary English words is never fuzzed into a
  single-word term ("cloud" stays "cloud", "preset" stays "preset").

The last guard needs an English wordlist. We use cracklib's small list
(present on Arch/Omarchy because pam depends on cracklib), then
``/usr/share/dict/words``. Without either, only same-letters fixes run
(casing and spacing, "code mux" -> "Codemux"); fuzzy rewrites are off.

Contract: :func:`correct` never raises. On any error it returns the input
unchanged — the post-processor must never eat a transcription. Stdlib
only; the wordlist is loaded lazily, once per process.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable
from unicodedata import category

WORDLIST_PATHS: tuple[Path, ...] = (
    Path("/usr/share/cracklib/cracklib-small"),
    Path("/usr/share/dict/words"),
)

# Fuzzy-match threshold for terms longer than 6 letters; shorter terms need
# 0.88, and spans that merge more tokens than the term has need 0.85.
THRESH = 0.80

# Safety caps. Real dictations are far below both; they only bound the
# worst case so a pathological input can't approach Voxtype's timeout.
MAX_TEXT_CHARS = 50_000
MAX_TERMS = 1000

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")
_NON_ALNUM = re.compile(r"[^a-z0-9]")

# A span may not start or end on a function word: that is how neighbours
# get eaten ("the pass page" must not become "passpage" with "the").
STOP = frozenset({
    "a", "an", "the", "and", "but", "or", "in", "on", "of", "to", "for",
    "with", "is", "it", "my", "as", "at", "by", "be", "then", "that",
    "this", "so", "if", "what", "you", "i", "we", "can",
})

_UNLOADED = object()
_words: object = _UNLOADED  # frozenset[str] | None once loaded


def squash(s: str) -> str:
    return _NON_ALNUM.sub("", s.lower())


def skeleton(s: str) -> str:
    """Rough phonetic key: squashed, a few spelling merges, vowels dropped."""
    s = squash(s)
    s = s.replace("ph", "f")
    s = re.sub(r"ck|q", "k", s)
    s = re.sub(r"c(?=[eiy])", "s", s)
    s = s.replace("c", "k").replace("z", "s").replace("x", "ks")
    return s[:1] + re.sub(r"[aeiouyhw]", "", s[1:])


def _load_wordlist(paths: Iterable[Path] = WORDLIST_PATHS) -> frozenset[str] | None:
    for path in paths:
        try:
            if path.is_file():
                text = path.read_text(errors="ignore").lower()
                words = frozenset(w for w in text.split() if w.isalpha())
                if words:
                    return words
        except OSError:
            continue
    return None


def wordlist() -> frozenset[str] | None:
    """The English wordlist (lazy, loaded once). None when none is installed."""
    global _words
    if _words is _UNLOADED:
        try:
            _words = _load_wordlist()
        except Exception:
            _words = None
    return _words  # type: ignore[return-value]


def _reset_wordlist() -> None:
    """Forget the cached wordlist (tests)."""
    global _words
    _words = _UNLOADED


def _clean_terms(terms: Iterable[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in terms:
        if not isinstance(t, str):
            continue
        t = t.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= MAX_TERMS:
            break
    return out


def correct(text: str, terms: Iterable[object]) -> str:
    """Rewrite misheard vocabulary spans in ``text``. Never raises."""
    try:
        if not text or not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
            return text
        clean = _clean_terms(terms)
        if not clean:
            return text
        return _fix(text, clean, wordlist())
    except Exception:
        return text


def _fix(text: str, terms: list[str], words: frozenset[str] | None) -> str:
    """Collect candidate spans for every term, then apply the best
    non-overlapping ones (same letters before fuzzy, longer spans first,
    then higher similarity). ``words=None`` disables fuzzy rewrites."""
    toks = list(TOKEN.finditer(text))
    if not toks:
        return text
    fuzzy = words is not None
    vocab_sq = {squash(t) for t in terms}
    n_text = len(text)

    # Per-term data, and the span widths each term wants:
    # multi-word terms: same or one fewer token ("Claudecode");
    # single-word terms: 1-2 tokens ("code mux").
    specs = []
    for term in terms:
        if not term.isascii():
            continue  # ASCII tokenization cannot safely match Unicode terms
        tq = squash(term)
        if len(tq) < 4:
            continue  # too short to fuzz safely (MCP)
        n = len(term.split())
        widths = tuple(w for w in ((n - 1, n) if n > 1 else (1, 2)) if w >= 1)
        specs.append((term, tq, skeleton(term), n, widths))
    if not specs:
        return text

    # Span facts don't depend on the term: compute each (i, w) once.
    span_cache: dict[tuple[int, int], tuple | None] = {}

    def span_info(i: int, w: int):
        key = (i, w)
        if key in span_cache:
            return span_cache[key]
        info = None
        lowered = [t.group(0).lower() for t in toks[i:i + w]]
        if not (w > 1 and (lowered[0] in STOP or lowered[-1] in STOP)):
            a, b = toks[i].start(), toks[i + w - 1].end()
            span = text[a:b]
            neighbors = text[max(0, a - 1):a] + text[b:b + 1]
            word_neighbor = any(
                c.isalnum() or category(c).startswith("M") for c in neighbors
            )
            url_like = (a > 0 and text[a - 1] in "./\\_") or (
                b < n_text - 1 and text[b] in "./\\_" and text[b + 1].isalnum()
            )
            whitespace_gaps = all(
                text[toks[j].end():toks[j + 1].start()].isspace()
                for j in range(i, i + w - 1)
            )
            if whitespace_gaps and not url_like and not word_neighbor:
                plain = bool(words) and all(x.strip("'") in words for x in lowered)
                info = (a, b, span, squash(span), plain)
        span_cache[key] = info
        return info

    cands = []
    for term, tq, tk, n, widths in specs:
        tlen = len(tq)
        max_len_diff = max(2, tlen // 3)
        base_need = 0.88 if tlen <= 6 else THRESH
        for i in range(len(toks)):
            for w in widths:
                if i + w > len(toks):
                    break
                info = span_info(i, w)
                if info is None:
                    continue
                a, b, span, sq, plain_english = info
                if sq == tq + "s" or sq == tq + "es":
                    continue  # plural of a vocab word: keep the user's plural
                if sq == tq:
                    # same letters (incl. already right): strongest claim
                    cands.append((2, w, 2.0, a, b, term))
                    continue
                if not fuzzy:
                    continue
                if sq in vocab_sq:
                    continue  # span is (a casing variant of) a different term
                if abs(len(sq) - tlen) > max_len_diff or len(sq) < 5:
                    continue  # short spans ("code") are real words too often
                if plain_english and not (w >= 2 and n >= 2):
                    continue  # "cloud", "super easy", "preset": real English
                if w == 1 and "-" in span:
                    continue  # hyphenated identifiers only match exactly
                sm = SequenceMatcher(None, sq, tq)
                if sm.quick_ratio() < 0.6:
                    continue  # r1 < 0.6 can never reach the threshold
                r1 = sm.ratio()
                r2 = SequenceMatcher(None, skeleton(span), tk).ratio() if r1 >= 0.6 else 0
                score = max(r1, 0.95 * r2)
                need = base_need
                if w > n:
                    need = max(need, 0.85)  # merging tokens: "PRs land" != Hyprland
                if score >= need:
                    cands.append((1, w, score, a, b, term))

    taken: list[tuple[int, int]] = []
    edits = []
    for tier, w, score, a, b, term in sorted(cands, reverse=True):
        if any(a < tb and ta < b for ta, tb in taken):
            continue
        taken.append((a, b))
        if text[a:b] != term:
            edits.append((a, b, term))
    for a, b, term in sorted(edits, reverse=True):
        text = text[:a] + term + text[b:]
    return text
