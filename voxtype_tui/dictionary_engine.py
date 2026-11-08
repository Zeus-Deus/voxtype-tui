"""Vexis-parity dictionary post-processor.

Two-stage pipeline applied to raw Voxtype output:

  1. Replacement rules (Fuzzy) — `compile_fuzzy_trigger_pattern` builds a
     Whisper-aware bounded-gap regex so a single trigger like
     ``"slash codemux release"`` matches every shape Whisper emits:
     verbatim, ``/codemux release``, ``/codemuxrelease``,
     ``/codemux-release``, ``/codemuxapi-release``, etc.

  2. Capitalization rules (Exact) — `escape_trigger_pattern` builds a
     strict whole-token regex. Runs after replacements to force proper
     casing without over-firing on compound identifiers like
     ``codemux-lang``.

Stage 2 (Vexis-style built-in spoken-command expansion — "slash X → /X",
"hash X → #X", etc.) is intentionally omitted: Voxtype's daemon already
does this via ``[text].spoken_punctuation``, so duplicating it here would
only risk double-conversion.

The engine is pure: no I/O, no config loading. Callers hand it a list of
rules and a string; it returns the processed string. Safe to instantiate
once per process and reuse; the compiled regex is cached inside.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

REPLACEMENT = "Replacement"
CAPITALIZATION = "Capitalization"
Category = Literal["Replacement", "Capitalization"]

# Max bytes of inserted word/separator chars tolerated in a fuzzy-gap match.
# Empirically tuned (from Vexis): 12 is enough to bridge Whisper hallucinations
# like an extra "api" or "the", but short enough not to bridge unrelated
# sentence fragments. Bumping this makes the matcher more permissive.
FUZZY_GAP_BUDGET = 12

# Map lowercased command phrases (one or two words) to the symbol Whisper
# emits when auto-formatting them. Used by the fuzzy compiler to turn a
# rule trigger like "slash foo" into a pattern that matches both the
# verbatim spoken phrase and the pre-formatted "/foo" shape.
_COMMAND_PHRASE_SYMBOLS: dict[str, str] = {
    "slash": "/",
    "hash": "#",
    "dot": ".",
    "dash": "-",
    "underscore": "_",
    "at sign": "@",
    "double colon": "::",
}


@dataclass(frozen=True)
class Rule:
    trigger: str
    replacement: str
    category: Category


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _is_token_boundary(text: str, start: int, end: int) -> bool:
    """Vexis-style neighbor check: a match is a valid token if neither
    immediate neighbor is a word character (or we're at a string edge).
    Works uniformly for triggers that start or end in non-word chars
    (c++, .net, c#), which plain `\b` cannot anchor.
    """
    left_ok = start == 0 or not _is_word_char(text[start - 1])
    right_ok = end >= len(text) or not _is_word_char(text[end])
    return left_ok and right_ok


def _escape_trigger_pattern(trigger: str) -> str | None:
    """Build an exact regex body for `trigger`. Internal whitespace is
    relaxed to `\\s+` so STT whitespace variance doesn't defeat the rule.
    Returns None when the trigger is empty.
    """
    trimmed = trigger.strip()
    if not trimmed:
        return None
    parts = [re.escape(p) for p in trimmed.split()]
    if not parts:
        return None
    return r"\s+".join(parts)


def _compile_fuzzy_trigger_pattern(trigger: str) -> str | None:
    """Build a Whisper-aware bounded-gap regex body for `trigger`.

    Steps mirror Vexis's `compile_fuzzy_trigger_pattern`:

      1. Tokenize on whitespace.
      2. Expand known command phrases (slash/hash/dot/…, plus two-word
         forms like "at sign", "double colon") into ``(?:spoken|symbol)``
         alternations so one rule matches both spoken and formatted forms.
      3. Regex-escape plain tokens.
      4. Join with a bounded gap ``(?:\\s+|[\\w\\-_/.]{0,12})`` — a single
         atom needs no gap.
    """
    trimmed = trigger.strip()
    if not trimmed:
        return None
    words = trimmed.split()
    if not words:
        return None

    atoms: list[str] = []
    i = 0
    while i < len(words):
        if i + 1 < len(words):
            two = f"{words[i].lower()} {words[i + 1].lower()}"
            symbol = _COMMAND_PHRASE_SYMBOLS.get(two)
            if symbol is not None:
                word_pat = rf"{re.escape(words[i])}\s+{re.escape(words[i + 1])}"
                atoms.append(f"(?:{word_pat}|{re.escape(symbol)})")
                i += 2
                continue
        lower = words[i].lower()
        symbol = _COMMAND_PHRASE_SYMBOLS.get(lower)
        if symbol is not None:
            atoms.append(f"(?:{re.escape(words[i])}|{re.escape(symbol)})")
            i += 1
            continue
        atoms.append(re.escape(words[i]))
        i += 1

    if not atoms:
        return None
    if len(atoms) == 1:
        return atoms[0]

    gap = rf"(?:\s+|[\w\-_/.]{{0,{FUZZY_GAP_BUDGET}}})"
    return gap.join(atoms)


class _RuleSet:
    """Alternation regex over a set of rules, with per-match boundary check.

    Rules are sorted longest-trigger-first so leftmost-first alternation
    gives longer triggers priority at any given position ("next js" wins
    over "js"). The combined pattern has no `\\b` anchors — the match
    callback does the boundary check on the full-match span's neighbors.
    """

    def __init__(self, rules: Iterable[tuple[str, str]], mode: Literal["exact", "fuzzy"]):
        pairs = sorted(rules, key=lambda p: len(p[0]), reverse=True)
        arms: list[str] = []
        replacements: list[str] = []
        for trigger, replacement in pairs:
            compiled = (
                _escape_trigger_pattern(trigger)
                if mode == "exact"
                else _compile_fuzzy_trigger_pattern(trigger)
            )
            if compiled is None:
                continue
            arms.append(f"({compiled})")
            replacements.append(replacement)
        self.replacements = replacements
        if arms:
            try:
                self.regex: re.Pattern[str] | None = re.compile(
                    "|".join(arms), re.IGNORECASE
                )
            except re.error:
                self.regex = None
        else:
            self.regex = None

    def __len__(self) -> int:
        return len(self.replacements)

    def apply(self, text: str) -> str:
        return self.apply_edits(text)[0]

    def apply_edits(self, text: str) -> tuple[str, list[Edit]]:
        """Like ``regex.sub``, also returning the fired rules as
        ``(start, end, replacement)`` edits on the input text."""
        if self.regex is None:
            return text, []
        out: list[str] = []
        edits: list[Edit] = []
        pos = 0
        for m in self.regex.finditer(text):
            idx: int | None = None
            for i in range(len(self.replacements)):
                if m.group(i + 1) is not None:
                    idx = i
                    break
            if idx is None or not _is_token_boundary(text, m.start(), m.end()):
                continue
            out.append(text[pos:m.start()])
            out.append(self.replacements[idx])
            edits.append((m.start(), m.end(), self.replacements[idx]))
            pos = m.end()
        if not edits:
            return text, []
        out.append(text[pos:])
        return "".join(out), edits


class DictionaryEngine:
    """Two-stage post-processor: fuzzy replacements then exact capitalization."""

    def __init__(self, rules: Iterable[Rule]):
        rules = list(rules)
        replacement_pairs = [
            (r.trigger, r.replacement) for r in rules if r.category == REPLACEMENT
        ]
        capitalization_pairs = [
            (r.trigger, r.replacement) for r in rules if r.category == CAPITALIZATION
        ]
        self._replacements = _RuleSet(replacement_pairs, "fuzzy")
        self._capitalizations = _RuleSet(capitalization_pairs, "exact")

    @property
    def replacement_count(self) -> int:
        return len(self._replacements)

    @property
    def capitalization_count(self) -> int:
        return len(self._capitalizations)

    def process(self, text: str) -> str:
        stage1 = self._replacements.apply(text)
        return self._capitalizations.apply(stage1)

    def process_spans(self, text: str) -> tuple[str, list[tuple[int, int]]]:
        """``process`` plus the character spans of the result that the
        rules produced (a later rule rewriting inside an earlier rule's
        output stays inside that span)."""
        stage1, e1 = self._replacements.apply_edits(text)
        spans = remap_spans([], e1)
        stage2, e2 = self._capitalizations.apply_edits(stage1)
        return stage2, remap_spans(spans, e2)


Edit = tuple  # (start, end, replacement) on the text before the edit


def remap_spans(spans: Iterable[tuple[int, int]], edits: Iterable[Edit],
                protect_new: bool = True) -> list[tuple[int, int]]:
    """Carry ``spans`` of a text through non-overlapping ``edits`` of it.

    A span grows to cover any edit that overlaps it (its output stays
    inside), others shift. With ``protect_new`` each edit's output becomes
    a span too. Returns sorted, merged spans of the edited text."""
    edits = sorted(edits)
    starts = []  # (old_a, old_b, new_a, new_b)
    delta = 0
    for a, b, new in edits:
        starts.append((a, b, a + delta, a + delta + len(new)))
        delta += len(new) - (b - a)

    def pos(x: int, end: bool) -> int:
        d = 0
        for a, b, na, nb in starts:
            if (a < x <= b) if end else (a <= x < b):
                return nb if end else na  # inside an edit: cover its output
            if b > x:
                break  # this and later edits lie after x
            d = nb - b  # cumulative shift after this edit
        return x + d

    out = []
    for s, e in spans:
        ns, ne = pos(s, False), pos(e, True)
        if ne > ns:
            out.append((ns, ne))
    if protect_new:
        out += [(na, nb) for _, _, na, nb in starts if nb > na]
    out.sort()
    merged: list[tuple[int, int]] = []
    for s, e in out:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def process(text: str, rules: Iterable[Rule]) -> str:
    """Convenience wrapper. Production code should reuse a
    `DictionaryEngine` instance."""
    return DictionaryEngine(rules).process(text)
