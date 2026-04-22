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
