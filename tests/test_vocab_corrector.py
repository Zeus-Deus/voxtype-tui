"""Unit tests for the always-on vocabulary corrector.

Fixtures are generic made-up sentences. The English wordlist is pinned to
a small in-test set so results don't depend on which wordlist the test
machine has installed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from voxtype_tui import vocab_corrector as vc

TERMS = [
    "Claude Code", "Codemux", "Hyprland", "passpage", "MCP",
    "omarchy-vm", "Superset", "TypeScript", "Claude",
]

# Plain-English words the guards need to recognise in these fixtures.
WORDS = frozenset("""
    a the and of to in on is it then open check use daily cloud nice
    preset values super set superset features land today hyper pass page
    clawed code type script fine great start now please two running
    tiny weather sunny warm happy
""".split())


@pytest.fixture(autouse=True)
def pinned_wordlist(monkeypatch):
    monkeypatch.setattr(vc, "_words", WORDS)
    yield
    vc._reset_wordlist()


def fix(text: str, terms=TERMS) -> str:
    return vc.correct(text, terms)


# ---------------------------------------------------------------------------
# Rewrites
# ---------------------------------------------------------------------------

def test_fixes_sound_alikes_and_spacing() -> None:
    src = "open clawed code and check code mux on hyperland, then the pass page"
    assert fix(src) == "open Claude Code and check Codemux on Hyprland, then the passpage"


def test_fixes_casing_of_multiword_term() -> None:
    assert fix("I use claude code daily") == "I use Claude Code daily"


def test_already_correct_text_is_unchanged() -> None:
    src = "Claude Code and Codemux on Hyprland"
    assert fix(src) == src


def test_merged_spelling_of_multiword_term() -> None:
    assert fix("open claudecode now") == "open Claude Code now"


def test_split_single_word_term() -> None:
    # "hyperr" is not an English word, so the two-token span may be fuzzed
    assert fix("the hyperr land setup") == "the Hyprland setup"
    # same letters split over two tokens: always fixed
    assert fix("type script is fine") == "TypeScript is fine"


def test_all_english_span_not_fuzzed_into_single_word_term() -> None:
    # "hyper" and "land" are both English words: never merged by similarity
    assert fix("the hyper land setup") == "the hyper land setup"


def test_casing_only_fix_for_english_word_term() -> None:
    # "superset" is an English word: same letters → casing fix is allowed.
    assert fix("a superset of features") == "a Superset of features"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_plain_english_single_word_not_fuzzed() -> None:
    assert fix("the cloud is nice") == "the cloud is nice"
    assert fix("preset values") == "preset values"


def test_spans_never_cross_punctuation() -> None:
    assert fix("we wrote code. Mux is a word") == "we wrote code. Mux is a word"
    assert fix("code mux. Then") == "Codemux. Then"


@pytest.mark.parametrize("words", [WORDS, None], ids=["wordlist", "no-wordlist"])
@pytest.mark.parametrize("src", [
    "edit code/mux",
    r"edit code\mux",
    "code — mux",
    "code – mux",
    "code - mux",
    "code (mux)",
    'code "mux"',
    "code_mux",
    "code + mux",
    "code@mux",
])
def test_spans_only_join_whitespace_separated_tokens(monkeypatch, words, src: str) -> None:
    monkeypatch.setattr(vc, "_words", words)
    assert fix(src, ["Codemux"]) == src


def test_urls_paths_and_file_names_left_alone() -> None:
    for src in (
        "visit app.codemux.org today",
        "edit claude.md now",
        "open ~/.codemux/config",
        "see src/hyperland/main.rs",
        "the file codemux_test.py",
    ):
        assert fix(src) == src, src


@pytest.mark.parametrize("words", [WORDS, None], ids=["wordlist", "no-wordlist"])
@pytest.mark.parametrize("src", [r"edit codemux\config", r"edit claude code\config"])
def test_backslash_path_suffix_is_left_alone(monkeypatch, words, src: str) -> None:
    monkeypatch.setattr(vc, "_words", words)
    assert fix(src, ["Codemux", "Claude Code"]) == src


def test_plural_kept() -> None:
    assert fix("two codemuxes running") == "two codemuxes running"


def test_hyphenated_identifier_only_matches_exactly() -> None:
    assert fix("start omarchy-vm now") == "start omarchy-vm now"
    # A different hyphenated token is never fuzzed into the term.
    assert fix("start omarchy-vn now") == "start omarchy-vn now"


def test_short_terms_never_touched() -> None:
    assert fix("use mcp please") == "use mcp please"
    assert fix("use mpc please") == "use mpc please"


def test_merging_tokens_needs_higher_similarity() -> None:
    assert fix("PRs land today") == "PRs land today"


def test_span_does_not_eat_function_words() -> None:
    assert fix("then the pass page") == "then the passpage"


def test_casing_variant_of_other_term_not_rewritten() -> None:
    # "claude" alone is its own vocab term; it must not become Claude Code.
    assert fix("ask claude about it") == "ask Claude about it"


# ---------------------------------------------------------------------------
# Wordlist handling
# ---------------------------------------------------------------------------

def test_without_wordlist_only_same_letters_fixes(monkeypatch) -> None:
    monkeypatch.setattr(vc, "_words", None)
    src = "open clawed code and check code mux on hyperland, then the pass page"
    # spacing/casing fixes still run; fuzzy (clawed→Claude, hyperland) do not
    assert fix(src) == "open clawed code and check Codemux on hyperland, then the passpage"


def test_wordlist_loaded_lazily_and_once(monkeypatch, tmp_path: Path) -> None:
    words = tmp_path / "words"
    words.write_text("Alpha\nbeta\nit's\ngamma2\n")
    calls = []
    real = vc._load_wordlist

    def counting(paths=(words,)):
        calls.append(1)
        return real(paths)

    vc._reset_wordlist()
    monkeypatch.setattr(vc, "_load_wordlist", counting)
    assert calls == []  # importing/patching does not load
    first = vc.wordlist()
    vc.correct("some text here", TERMS)
    vc.correct("more text here", TERMS)
    assert vc.wordlist() is first
    assert calls == [1]
    assert first == frozenset({"alpha", "beta"})


def test_load_wordlist_falls_back_in_order(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    empty = tmp_path / "empty"
    empty.write_text("\n")
    second = tmp_path / "second"
    second.write_text("hello\nworld\n")
    assert vc._load_wordlist((missing, empty, second)) == frozenset({"hello", "world"})
    assert vc._load_wordlist((missing,)) is None


def test_wordlist_none_when_loader_raises(monkeypatch) -> None:
    vc._reset_wordlist()
    monkeypatch.setattr(vc, "_load_wordlist", lambda: 1 / 0)
    assert vc.wordlist() is None


# ---------------------------------------------------------------------------
# Never raises
# ---------------------------------------------------------------------------

def test_empty_inputs_pass_through() -> None:
    assert fix("") == ""
    assert fix("hello there", []) == "hello there"
    assert fix("...", TERMS) == "..."


def test_bad_terms_are_skipped() -> None:
    assert vc.correct("check code mux", [None, 3, "", "  ", "Codemux"]) == "check Codemux"


def test_non_string_input_returned_unchanged() -> None:
    assert vc.correct(None, TERMS) is None  # type: ignore[arg-type]
    assert vc.correct(b"code mux", TERMS) == b"code mux"  # type: ignore[arg-type]


def test_internal_error_returns_input(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(vc, "_fix", boom)
    assert fix("check code mux") == "check code mux"


def test_terms_none_returns_input() -> None:
    assert vc.correct("check code mux", None) == "check code mux"  # type: ignore[arg-type]


def test_oversized_input_returned_unchanged() -> None:
    big = "code mux " * (vc.MAX_TEXT_CHARS // 9 + 1)
    assert fix(big) == big


@pytest.mark.parametrize("words", [WORDS, None], ids=["wordlist", "no-wordlist"])
@pytest.mark.parametrize("src,terms,expected", [
    ("Überall", ["Überall"], "Überall"),
    ("überall", ["Überall"], "überall"),
    ("naive", ["naïve"], "naive"),
    ("use code mux", ["Codemux", "ÜCodemux"], "use Codemux"),
])
def test_non_ascii_vocabulary_is_skipped(monkeypatch, words, src, terms, expected) -> None:
    monkeypatch.setattr(vc, "_words", words)
    assert fix(src, terms) == expected


@pytest.mark.parametrize("words", [WORDS, None], ids=["wordlist", "no-wordlist"])
@pytest.mark.parametrize("neighbor", ["é", "中", "١", "\u0301", "\u0903", "\u20dd"])
@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("phrase", ["codemux", "code mux"])
def test_unicode_word_neighbors_block_partial_matches(monkeypatch, words, neighbor, side, phrase) -> None:
    monkeypatch.setattr(vc, "_words", words)
    src = neighbor + phrase if side == "before" else phrase + neighbor
    assert fix(src, ["Codemux"]) == src


@pytest.mark.parametrize("words", [WORDS, None], ids=["wordlist", "no-wordlist"])
@pytest.mark.parametrize("src,expected", [
    ("code mux", "Codemux"),
    ("code\tmux", "Codemux"),
    ("code\u00a0mux", "Codemux"),
    ("(code mux)!", "(Codemux)!"),
    ("code mux, then", "Codemux, then"),
    ("code-mux", "Codemux"),
    ("OMARCHY-VM", "omarchy-vm"),
    ("omarchy-vn", "omarchy-vn"),
])
def test_spacing_and_exact_identifier_behavior_is_preserved(monkeypatch, words, src, expected) -> None:
    monkeypatch.setattr(vc, "_words", words)
    assert fix(src, ["Codemux", "omarchy-vm"]) == expected


@pytest.mark.parametrize("words", [WORDS, None], ids=["wordlist", "no-wordlist"])
@pytest.mark.parametrize("separator", [".", ",", "!", "?", ";", ":"])
def test_sentence_punctuation_blocks_token_joining(monkeypatch, words, separator) -> None:
    monkeypatch.setattr(vc, "_words", words)
    src = f"code{separator} mux"
    assert fix(src, ["Codemux"]) == src


def test_unicode_text_is_safe() -> None:
    src = "café — naïve code mux 🎉 überall"
    assert fix(src) == "café — naïve Codemux 🎉 überall"
