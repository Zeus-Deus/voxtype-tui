"""Unit tests for the sidecar metadata layer."""
from __future__ import annotations

from pathlib import Path

from voxtype_tui import sidecar


# ---------------------------------------------------------------------------
# Load / save round-trip
# ---------------------------------------------------------------------------

def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    sc = sidecar.load(tmp_path / "nope.json")
    assert sc.vocabulary == []
    assert sc.replacements == []


def test_load_corrupt_json_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{ not valid json")
    sc = sidecar.load(p)
    assert sc.vocabulary == []
    assert sc.replacements == []


def test_save_load_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "metadata.json"
    sc = sidecar.Sidecar(
        vocabulary=[
            sidecar.VocabEntry(phrase="Claude Code"),
            sidecar.VocabEntry(phrase="Omarchy", notes="distro"),
        ],
        replacements=[
            sidecar.ReplacementEntry(from_text="vox type", category="Replacement"),
            sidecar.ReplacementEntry(from_text="type script", category="Capitalization"),
        ],
    )
    sidecar.save_atomic(sc, p)
    reloaded = sidecar.load(p)
    assert [v.phrase for v in reloaded.vocabulary] == ["Claude Code", "Omarchy"]
    assert reloaded.vocabulary[1].notes == "distro"
    assert [(r.from_text, r.category) for r in reloaded.replacements] == [
        ("vox type", "Replacement"),
        ("type script", "Capitalization"),
    ]


def test_load_tolerates_legacy_from_field(tmp_path: Path) -> None:
    """If someone hand-edits with 'from' instead of 'from_text', still load."""
    p = tmp_path / "metadata.json"
    p.write_text(
        '{"version":1,"vocabulary":[],"replacements":[{"from":"x","category":"Capitalization","added_at":"2026-01-01T00:00:00+00:00"}]}'
    )
    sc = sidecar.load(p)
    assert sc.replacements[0].from_text == "x"
    assert sc.replacements[0].category == "Capitalization"


def test_load_migrates_legacy_command_category_to_replacement(tmp_path: Path) -> None:
    """Older sidecar files tagged entries with 'Command', a category we've
    since folded into 'Replacement'. Loading such a file must silently
    rewrite the tag — no user prompt, no exception, no warning."""
    p = tmp_path / "metadata.json"
    p.write_text(
        '{"version":1,"vocabulary":[],"replacements":['
        '{"from_text":"slash deploy","category":"Command","added_at":"2026-01-01T00:00:00+00:00"}'
        ']}'
    )
    sc = sidecar.load(p)
    assert len(sc.replacements) == 1
    assert sc.replacements[0].category == "Replacement"
    assert sc.replacements[0].from_text == "slash deploy"
    # "Command" must no longer be a valid category at all
    assert "Command" not in sidecar.CATEGORIES


def test_categories_exposes_two_options() -> None:
    """UI code iterates sidecar.CATEGORIES to build the Dictionary category
    Select; this test locks in the 2-option shape so the UI stays in sync
    with the sidecar contract."""
    assert sidecar.CATEGORIES == ("Replacement", "Capitalization")


def test_load_rejects_unknown_category_defaults(tmp_path: Path) -> None:
    p = tmp_path / "metadata.json"
    p.write_text(
        '{"version":1,"vocabulary":[],"replacements":[{"from_text":"x","category":"Bogus","added_at":"2026-01-01T00:00:00+00:00"}]}'
    )
    sc = sidecar.load(p)
    assert sc.replacements[0].category == sidecar.DEFAULT_CATEGORY


# ---------------------------------------------------------------------------
# Vocab reconciliation
# ---------------------------------------------------------------------------

def test_reconcile_vocab_matches_no_warning() -> None:
    sc_vocab = [sidecar.VocabEntry(phrase="A"), sidecar.VocabEntry(phrase="B")]
    initial = "A, B"
    out, warns = sidecar.reconcile_vocab(sc_vocab, initial)
    assert [v.phrase for v in out] == ["A", "B"]
    assert warns == []


def test_reconcile_vocab_diverges_rebuilds_from_disk() -> None:
    sc_vocab = [sidecar.VocabEntry(phrase="Stale", notes="old notes")]
    initial = "Alpha, Beta, Gamma"
    out, warns = sidecar.reconcile_vocab(sc_vocab, initial)
    assert [v.phrase for v in out] == ["Alpha", "Beta", "Gamma"]
    assert warns, "expected a divergence warning"
    # notes on 'Stale' shouldn't carry over to a freshly-created entry
    assert all(v.notes is None for v in out)


def test_reconcile_vocab_preserves_metadata_on_matching_phrase() -> None:
    sc_vocab = [
        sidecar.VocabEntry(phrase="Alpha", notes="greek letter"),
        sidecar.VocabEntry(phrase="Stale"),
    ]
    initial = "Alpha, Beta"  # Alpha still present, Stale gone, Beta new
    out, warns = sidecar.reconcile_vocab(sc_vocab, initial)
    assert [v.phrase for v in out] == ["Alpha", "Beta"]
    assert out[0].notes == "greek letter"
    assert warns  # there is divergence (Stale dropped, Beta added)


def test_reconcile_vocab_both_empty_no_warn() -> None:
    out, warns = sidecar.reconcile_vocab([], None)
    assert out == []
    assert warns == []


def test_reconcile_vocab_empty_disk_clears_sidecar() -> None:
    sc_vocab = [sidecar.VocabEntry(phrase="X")]
    out, warns = sidecar.reconcile_vocab(sc_vocab, None)
    assert out == []
    assert warns  # user deleted all vocab from disk


# ---------------------------------------------------------------------------
# Replacement reconciliation
# ---------------------------------------------------------------------------

def test_reconcile_reps_preserves_category() -> None:
    sc_reps = [sidecar.ReplacementEntry(from_text="vox type", category="Capitalization")]
    cfg = {"vox type": "voxtype"}
    out, warns = sidecar.reconcile_replacements(sc_reps, cfg)
    assert [(r.from_text, r.category) for r in out] == [("vox type", "Capitalization")]
    assert warns == []


def test_reconcile_reps_normalizes_unknown_category_without_warning() -> None:
    """A sidecar entry with a category that's no longer valid (e.g. an
    in-memory 'Command' that slipped past load-time normalization, or any
    other invalid tag) must be quietly reset to the default. This is NOT a
    divergence warning — it's a schema migration."""
    # Construct directly; bypasses load()'s normalization
    sc_reps = [sidecar.ReplacementEntry(from_text="vox type", category="Command")]
    cfg = {"vox type": "voxtype"}
    out, warns = sidecar.reconcile_replacements(sc_reps, cfg)
    assert [(r.from_text, r.category) for r in out] == [("vox type", "Replacement")]
    assert warns == []


def test_reconcile_reps_unknown_key_defaults_to_replacement() -> None:
    sc_reps: list[sidecar.ReplacementEntry] = []
    cfg = {"cloud code": "Claude Code"}
    out, warns = sidecar.reconcile_replacements(sc_reps, cfg)
    assert out[0].category == sidecar.DEFAULT_CATEGORY
    assert any("defaulted" in w for w in warns)


def test_reconcile_reps_drops_orphans() -> None:
    sc_reps = [
        sidecar.ReplacementEntry(from_text="ghost", category="Capitalization"),
        sidecar.ReplacementEntry(from_text="vox type", category="Replacement"),
    ]
    cfg = {"vox type": "voxtype"}
    out, warns = sidecar.reconcile_replacements(sc_reps, cfg)
    assert [r.from_text for r in out] == ["vox type"]
    assert any("orphan" in w.lower() or "dropped" in w.lower() for w in warns)


def test_reconcile_reps_mixed_add_drop() -> None:
    sc_reps = [
        sidecar.ReplacementEntry(from_text="ghost", category="Capitalization"),
        sidecar.ReplacementEntry(from_text="vox type", category="Capitalization"),
    ]
    cfg = {"vox type": "voxtype", "cloud code": "Claude Code"}
    out, warns = sidecar.reconcile_replacements(sc_reps, cfg)
    # ghost dropped, vox type preserved, cloud code defaulted
    cats = {r.from_text: r.category for r in out}
    assert cats == {"vox type": "Capitalization", "cloud code": "Replacement"}
    assert len(warns) == 2  # one "defaulted", one "dropped"


# ---------------------------------------------------------------------------
# Building the initial_prompt string from vocab
# ---------------------------------------------------------------------------

def test_build_initial_prompt_joins_with_comma_space() -> None:
    vocab = [
        sidecar.VocabEntry(phrase="A"),
        sidecar.VocabEntry(phrase="B"),
        sidecar.VocabEntry(phrase="C"),
    ]
    assert sidecar.build_initial_prompt(vocab) == "A, B, C"


def test_build_initial_prompt_empty() -> None:
    assert sidecar.build_initial_prompt([]) == ""


def test_parse_initial_prompt_round_trip() -> None:
    vocab = [sidecar.VocabEntry(phrase=p) for p in ["Foo", "Bar", "Baz"]]
    s = sidecar.build_initial_prompt(vocab)
    assert sidecar.parse_initial_prompt(s) == ["Foo", "Bar", "Baz"]


def test_parse_initial_prompt_tolerates_extra_whitespace() -> None:
    assert sidecar.parse_initial_prompt("  A ,  B,C  ") == ["A", "B", "C"]


def test_parse_initial_prompt_ignores_empty_segments() -> None:
    assert sidecar.parse_initial_prompt(",A,,B,") == ["A", "B"]
