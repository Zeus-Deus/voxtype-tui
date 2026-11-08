"""Unit tests for the Vexis-parity dictionary engine.

Every assertion below mirrors a pinned Vexis behavior (see
`vexis/src-tauri/src/dictionary/mod.rs` tests). If these break we've
regressed away from parity.
"""
from __future__ import annotations

from voxtype_tui.dictionary_engine import (
    CAPITALIZATION,
    REPLACEMENT,
    DictionaryEngine,
    Rule,
    process,
)


# ---------------------------------------------------------------------------
# Stage 1 — fuzzy replacements
# ---------------------------------------------------------------------------

def test_simple_replacement() -> None:
    rules = [Rule("cloud code", "Claude Code", REPLACEMENT)]
    assert process("i love cloud code so much", rules) == "i love Claude Code so much"


def test_case_insensitive() -> None:
    rules = [Rule("cloud code", "Claude Code", REPLACEMENT)]
    for src in ("i love CLOUD CODE", "Cloud Code is great", "cLoUd CoDe rules"):
        assert "Claude Code" in process(src, rules)


def test_respects_word_boundaries() -> None:
    # `react` must not match inside `unreactive`.
    rules = [Rule("react", "React", REPLACEMENT)]
    assert process("unreactive code is inert", rules) == "unreactive code is inert"


def test_boundary_at_end() -> None:
    rules = [Rule("code", "CODE", REPLACEMENT)]
    assert process("codes are codes", rules) == "codes are codes"


def test_multiple_replacements_in_one_string() -> None:
    rules = [
        Rule("typescript", "TypeScript", REPLACEMENT),
        Rule("javascript", "JavaScript", REPLACEMENT),
    ]
    assert process("i write typescript and javascript", rules) == (
        "i write TypeScript and JavaScript"
    )


def test_longer_trigger_wins_over_shorter() -> None:
    rules = [
        Rule("js", "JavaScript", REPLACEMENT),
        Rule("next js", "Next.js", REPLACEMENT),
    ]
    assert process("we use next js here", rules) == "we use Next.js here"


def test_empty_rule_list_is_passthrough() -> None:
    assert process("nothing changes here", []) == "nothing changes here"


def test_empty_trigger_is_skipped() -> None:
    rules = [Rule("", "Nope", REPLACEMENT)]
    assert process("nothing changes", rules) == "nothing changes"


def test_internal_whitespace_is_flexible() -> None:
    rules = [Rule("next js", "Next.js", REPLACEMENT)]
    assert process("we use next   js today", rules) == "we use Next.js today"


# ---------------------------------------------------------------------------
# Stage 1 — Whisper-aware fuzzy matching
# ---------------------------------------------------------------------------

def test_fuzzy_matches_whisper_variants() -> None:
    """A single rule matches every shape Whisper produces."""
    rules = [Rule("slash codemux release", "/codemux-release", REPLACEMENT)]
    variants = [
        ("run slash codemux release now", "run /codemux-release now"),
        ("run /codemux-release now", "run /codemux-release now"),
        ("run /codemuxrelease now", "run /codemux-release now"),
        ("run /codemux release now", "run /codemux-release now"),
    ]
    for src, want in variants:
        assert process(src, rules) == want, f"failed on {src!r}"


def test_fuzzy_respects_outer_word_boundaries() -> None:
    """Fuzzy matching must not fire inside a larger compound identifier."""
    rules = [Rule("codemux release", "/codemux-release", REPLACEMENT)]
    assert process("subcodemuxreleasely matters", rules) == "subcodemuxreleasely matters"


def test_fuzzy_command_phrase_slash() -> None:
    """Trigger `slash X` matches both spoken and auto-formatted Whisper output."""
    rules = [Rule("slash compact", "/compact", REPLACEMENT)]
    assert process("please slash compact now", rules) == "please /compact now"
    assert process("please /compact now", rules) == "please /compact now"


def test_fuzzy_does_not_bridge_sentence_fragments() -> None:
    """The fuzzy gap excludes whitespace runs, so `codemux ... release`
    across unrelated words must NOT match as one rule."""
    rules = [Rule("codemux release", "/codemux-release", REPLACEMENT)]
    assert (
        process("codemux is a great tool and also release", rules)
        == "codemux is a great tool and also release"
    )


# ---------------------------------------------------------------------------
# Stage 3 — exact capitalization
# ---------------------------------------------------------------------------

def test_capitalization_basic() -> None:
    rules = [Rule("rust", "Rust", CAPITALIZATION)]
    assert process("i code in rust every day", rules) == "i code in Rust every day"


def test_capitalization_idempotent() -> None:
    rules = [Rule("rust", "Rust", CAPITALIZATION)]
    assert process("I code in Rust every day", rules) == "I code in Rust every day"


def test_capitalization_handles_mixed_case() -> None:
    rules = [Rule("react", "React", CAPITALIZATION)]
    assert process("I love ReAcT and REACT and react", rules) == (
        "I love React and React and React"
    )


def test_capitalization_respects_word_boundaries() -> None:
    """`rust` must not match inside `trustworthy`."""
    rules = [Rule("rust", "Rust", CAPITALIZATION)]
    assert process("trustworthy rust truster", rules) == "trustworthy Rust truster"


def test_capitalization_non_word_edge_trigger() -> None:
    """Triggers with non-word edge characters must still match (`\\b` doesn't,
    our neighbor-check does). This is why we don't use `\\b` anchors."""
    rules = [Rule(".net", ".NET", CAPITALIZATION)]
    assert process("I write .net code", rules) == "I write .NET code"


# ---------------------------------------------------------------------------
# Stage ordering & full pipeline
# ---------------------------------------------------------------------------

def test_full_pipeline_end_to_end() -> None:
    rules = [
        Rule("cloud code", "Claude Code", REPLACEMENT),
        Rule("typescript", "TypeScript", CAPITALIZATION),
    ]
    got = process("i wrote some typescript in cloud code today", rules)
    assert got == "i wrote some TypeScript in Claude Code today"


def test_stage3_runs_after_stage1() -> None:
    """Stage 1 output flows into Stage 3. If Stage 1 emits `Codemux`, then
    a Capitalization rule on `codemux → Codemux` stays idempotent."""
    rules = [
        Rule("slash codemux release", "/codemux-release", REPLACEMENT),
        Rule("codemux", "Codemux", CAPITALIZATION),
    ]
    # Stage 1 emits `/codemux-release`, Stage 3 capitalizes `codemux` inside it.
    assert process("run slash codemux release", rules) == "run /Codemux-release"


def test_counts() -> None:
    rules = [
        Rule("one", "1", REPLACEMENT),
        Rule("two", "2", REPLACEMENT),
        Rule("three", "3", CAPITALIZATION),
    ]
    eng = DictionaryEngine(rules)
    assert eng.replacement_count == 2
    assert eng.capitalization_count == 1


def test_engine_is_reusable() -> None:
    """One engine instance handles many inputs without recompiling."""
    eng = DictionaryEngine([Rule("cloud code", "Claude Code", REPLACEMENT)])
    for _ in range(50):
        assert eng.process("cloud code") == "Claude Code"


def test_remap_spans_grows_over_and_shifts_past_edits():
    from voxtype_tui.dictionary_engine import remap_spans

    # "aa XXXX bb" with span over "XXXX bb" (3, 10)
    assert remap_spans([(3, 10)], [(3, 7, "Y")]) == [(3, 7)]       # edit at span start
    assert remap_spans([(3, 10)], [(0, 2, "zzzz")], protect_new=False) == [(5, 12)]  # shift
    assert remap_spans([(0, 2)], [(3, 7, "Y")]) == [(0, 2), (3, 4)]  # edit after: unchanged
    assert remap_spans([(3, 6)], [(5, 9, "QQ")]) == [(3, 7)]       # overlapping end: grow
    assert remap_spans([(3, 6)], [(5, 9, "QQ")], protect_new=False) == [(3, 7)]
    assert remap_spans([(4, 6)], [(0, 2, "")], protect_new=False) == [(2, 4)]


def test_process_spans_cover_composed_rule_output():
    rules = [Rule("project", "code mux Omashi", REPLACEMENT),
             Rule("code mux", "Codemux", CAPITALIZATION)]
    text, spans = DictionaryEngine(rules).process_spans("the project now")
    assert text == "the Codemux Omashi now"
    assert spans == [(4, 18)] and text[4:18] == "Codemux Omashi"


def _remap_reference(spans, edits, protect_new=True):
    """The straightforward per-endpoint scan (quadratic), kept as the oracle."""
    edits = sorted(edits)
    starts, delta = [], 0
    for a, b, new in edits:
        starts.append((a, b, a + delta, a + delta + len(new)))
        delta += len(new) - (b - a)

    def pos(x, end):
        d = 0
        for a, b, na, nb in starts:
            if (a < x <= b) if end else (a <= x < b):
                return nb if end else na
            if b > x:
                break
            d = nb - b
        return x + d

    out = [(pos(s, False), pos(e, True)) for s, e in spans]
    out = [(s, e) for s, e in out if e > s]
    if protect_new:
        out += [(na, nb) for _, _, na, nb in starts if nb > na]
    out.sort()
    merged = []
    for s, e in out:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def test_remap_spans_matches_the_reference_on_random_edits():
    import random

    from voxtype_tui.dictionary_engine import remap_spans

    rng = random.Random(7)
    for _ in range(3000):
        n = rng.randint(0, 40)
        cuts = sorted(rng.sample(range(n + 1), k=min(n + 1, 2 * rng.randint(0, 6))))
        edits = [(cuts[i], cuts[i + 1], "x" * rng.randint(0, 4)) for i in range(0, len(cuts) - 1, 2)]
        if rng.random() < 0.3 and n:  # zero-width insertions too
            at = rng.randint(0, n)
            if all(not (a < at < b) and a != at and b != at for a, b, _ in edits):
                edits.append((at, at, "ins"))
        spans = []
        for _ in range(rng.randint(0, 5)):
            a = rng.randint(0, n)
            spans.append((a, rng.randint(a, n)))
        for pn in (True, False):
            assert remap_spans(spans, edits, protect_new=pn) == _remap_reference(spans, edits, pn), (
                spans, edits, pn)


def test_process_spans_stays_linear_on_long_repetitive_text():
    """Both stages firing on every word of a long transcript: carrying the
    spans costs about as much as the rewrite itself, never quadratic time
    (the post-process has a timeout)."""
    import time

    eng = DictionaryEngine([Rule("foo", "bar", REPLACEMENT), Rule("bar", "Bar", CAPITALIZATION)])
    text = "foo " * 12500  # 50,000 characters

    def best(fn):
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            fn(text)
            times.append(time.perf_counter() - t0)
        return min(times)

    out, spans = eng.process_spans(text)
    assert out == eng.process(text) == "Bar " * 12500
    assert len(spans) == 12500 and all(out[a:b] == "Bar" for a, b in spans)
    plain = best(eng.process)
    with_spans = best(eng.process_spans)
    assert with_spans < 1.0
    assert with_spans < 3 * plain + 0.01, (with_spans, plain)
