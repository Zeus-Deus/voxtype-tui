"""Learned vocabulary: guards, apply stage, decisions, isolation.

Every test runs with HOME/XDG_* pointed at tmp (autouse fixture below plus
conftest's XDG isolation), so nothing touches real files."""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from voxtype_tui import learned as L
from voxtype_tui import vocab_corrector


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from voxtype_tui import cli_postprocess
    monkeypatch.setattr(cli_postprocess, "SIDECAR_PATH", tmp_path / "config" / "voxtype-tui" / "metadata.json")
    words = frozenset("""the a on of to in is this that default dictation option
        meet me at noon tuesday friday austin read yesterday cloud code
        hello there their every day everyday one two three four five six seven
        eight nine ten great i love it and bar rocks today we are going build
        something with computers quite good for writing really""".split())
    monkeypatch.setattr(vocab_corrector, "_words", words)
    yield


def rec(final, rid="r1", cls="org.gnome.TextEditor"):
    return {"id": rid, "final": final, "raw": final, "app": {"class": cls}}


def features(tmp_path, **learning):
    p = Path(os.environ["XDG_CONFIG_HOME"]) / "voxtype-tui" / "features.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": 1, "learning": learning}))


def sidecar(vocab=(), reps=()):
    from voxtype_tui import cli_postprocess
    p = Path(cli_postprocess.SIDECAR_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "version": 3,
        "vocabulary": [{"phrase": v} for v in vocab],
        "replacements": [{"from_text": f, "to_text": t, "category": "Replacement"} for f, t in reps],
    }))


# ---------------------------------------------------------------- guards

def test_sounds_like_from_fix_is_active_and_applies():
    out = L.submit_correction(rec("the default dictation option on Omashi."),
                              "the default dictation option on Omarchy.", "fix")
    assert [(c["heard"], c["written"], c["kind"]) for c in out["candidates"]] == \
        [("Omashi", "Omarchy", "sounds_like")]
    assert out["applied"][0]["state"] == "active"
    text, hits = L.apply("I love Omashi. omashi rocks")
    assert text == "I love Omarchy. Omarchy rocks"
    assert hits == [out["applied"][0]["id"]] * 2


def test_spelling_for_english_heard_word():
    out = L.submit_correction(rec("I read Austin yesterday"), "I read Austen yesterday", "fix")
    assert out["candidates"][0]["kind"] == "spelling"
    # an English heard word is never blindly replaced
    assert L.apply("I flew to Austin today")[0] == "I flew to Austin today"


def test_semantic_edit_rejected():
    out = L.submit_correction(rec("Meet me on Tuesday at noon"), "Meet me on Friday at noon", "fix")
    assert out["applied"] == []
    assert "too different" in out["skipped"][0]["reason"]


def test_whole_sentence_rewrite_rejected():
    out = L.submit_correction(rec("one two three four five six"),
                              "seven eight nine ten five six", "fix")
    assert out["applied"] == [] and "rewrite" in out["reason"]


def test_passive_rewrite_fraction_is_stricter():
    # 2 of 5 words changed: fine for an explicit fix, a rewrite when passive
    o = "this is Omashi and Codemucks"
    e = "this is Omarchy and Codemux"
    assert "rewrite" in L.submit_correction(rec(o, "p1"), e, "atspi")["reason"]
    assert L.submit_correction(rec(o, "p2"), e, "fix")["applied"]


def test_password_manager_window_refused_for_passive():
    out = L.submit_correction(rec("on Omashi", cls="1Password"), "on Omarchy", "atspi")
    assert out["applied"] == [] and "password" in out["reason"]
    assert not L.store_path().exists()


def test_ordinary_word_and_short_and_numbers_skipped():
    for o, e in [("hello their friend", "hello there friend"),
                 ("we go to Qx now", "we go to Qz now"),
                 ("call 1234 please", "call 1243 please")]:
        out = L.submit_correction(rec(o), e, "fix")
        assert out["applied"] == [], (o, e, out)


def test_trailing_continuation_is_not_a_rewrite():
    out = L.submit_correction(rec("default option on Omashi."),
                              "default option on Omarchy. And then I kept typing more words here",
                              "atspi")
    assert out["applied"] and out["applied"][0]["written"] == "Omarchy"


def test_user_words_are_never_relearned_or_changed(tmp_path):
    sidecar(vocab=["Hyprland"], reps=[("omashi", "Omarchy OS")])
    from voxtype_tui import cli_postprocess
    before = Path(cli_postprocess.SIDECAR_PATH).read_bytes()
    out = L.submit_correction(rec("I use Omashi and Hyperland"), "I use Omarchy and Hyprland", "fix")
    reasons = {s["heard"]: s["reason"] for s in out["skipped"]}
    assert "covered by your own words" in reasons["Omashi"]
    # a mishearing of one of the user's words may become a learned alias
    # pointing AT the user's word; the user's own list is not touched
    assert [(c["heard"], c["written"]) for c in out["candidates"]] == [("Hyperland", "Hyprland")]
    spelled = L.submit_correction(rec("I use hyprland", "r2"), "I use Hyprland", "fix")
    assert spelled["applied"] == [] and "your own words" in spelled["skipped"][0]["reason"]
    after = Path(cli_postprocess.SIDECAR_PATH).read_bytes()
    assert before == after


def test_passive_ambiguous_needs_two_corrections():
    # spelling kind (English heard) from a passive source stays suggested
    o, e = "I read Austin yesterday", "I read Austen yesterday"
    a = L.submit_correction(rec(o, "a1"), e, "atspi")["applied"][0]
    assert a["state"] == "suggested"
    again_same_record = L.submit_correction(rec(o, "a1"), e, "atspi")["applied"][0]
    assert again_same_record["state"] == "suggested" and again_same_record["count"] == 1
    b = L.submit_correction(rec(o, "a2"), e, "atspi")["applied"][0]
    assert b["state"] == "active" and b["count"] == 2


def test_passive_window_is_30_days():
    o, e = "I read Austin yesterday", "I read Austen yesterday"
    old = datetime.now(timezone.utc) - timedelta(days=40)
    L.submit_correction(rec(o, "a1"), e, "atspi", now=old)
    b = L.submit_correction(rec(o, "a2"), e, "atspi")["applied"][0]
    assert b["state"] == "suggested"


def test_rejected_is_a_tombstone():
    out = L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")
    eid = out["applied"][0]["id"]
    assert L.reject(eid)["state"] == "rejected"
    assert L.apply("on Omashi")[0] == "on Omashi"
    again = L.submit_correction(rec("on Omashi", "r9"), "on Omarchy", "fix")
    assert again["applied"] == [] and "rejected" in again["skipped"][0]["reason"]


# ---------------------------------------------------------------- apply

def test_apply_never_rewrites_inside_protected_spans():
    L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")
    # The user's dictionary produced "Omashi Labs" (it contains the learned
    # alias's heard token): it must stay as the user's rule wrote it.
    text = "Welcome to Omashi Labs, said Omashi"
    out, hits = L.apply(text, [(11, 22)])
    assert out == "Welcome to Omashi Labs, said Omarchy"
    out2, _ = L.apply(text, user_phrases=["Omashi Labs"])
    assert out2 == "Welcome to Omashi Labs, said Omarchy"


def test_apply_possessive_and_multiword_alias():
    L.submit_correction(rec("ask code max now"), "ask Codemax now", "fix", kind="replace")
    L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")
    assert L.apply("Omashi's bar and code max.")[0] == "Omarchy's bar and Codemax."


def test_apply_fuzzy_variant_via_written_term():
    L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")
    assert L.apply("default option on Omachi today")[0] == "default option on Omarchy today"


def test_apply_off_when_learning_disabled(tmp_path):
    L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")
    features(tmp_path, enabled=False)
    assert L.apply("on Omashi") == ("on Omashi", [])
    assert L.submit_correction(rec("on Omashi", "x"), "on Omarchy", "fix")["reason"] == "learning is off"
    features(tmp_path, enabled=True)
    assert L.apply("on Omashi")[0] == "on Omarchy"


def test_apply_fail_open_on_corrupt_files():
    L.compiled_path().parent.mkdir(parents=True, exist_ok=True)
    L.compiled_path().write_text("{not json")
    assert L.apply("on Omashi") == ("on Omashi", [])
    L.compiled_path().write_text(json.dumps({"aliases": {"omashi": None}}))
    assert L.apply("on Omashi") == ("on Omashi", [])


def test_apply_is_fast_with_500_entries():
    r = L._bench(500, 60)
    assert r["p95_ms"] < 1.0, r


def test_no_learned_file_means_identical_output():
    assert L.apply("anything Omashi") == ("anything Omashi", [])


# ---------------------------------------------------------------- decisions

def test_forget_reset_and_files_are_private():
    a = L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")["applied"][0]
    L.submit_correction(rec("ask code max now", "r2"), "ask Codemax now", "fix", kind="replace")
    assert stat.S_IMODE(L.store_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(L.store_path().parent.stat().st_mode) == 0o700
    assert L.forget(a["id"]) is True
    assert L.apply("on Omashi")[0] == "on Omashi"
    assert L.reset() == 1
    assert L.list_entries()["entries"] == []
    assert L.apply("code max")[0] == "code max"


def test_undo_first_time_entry_removes_it():
    a = L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")["applied"][0]
    assert L.undo(a["id"], "r1")
    assert L.list_entries()["entries"] == []


def test_promote_uses_user_add_and_marks_promoted():
    a = L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")["applied"][0]
    seen = []
    out = L.promote(a["id"], add_to_user=lambda e: (seen.append(L.user_additions(e)),
                                                    L.user_additions(e))[1])
    assert seen == [{"vocab": [], "rules": [["Omashi", "Omarchy"]]}]
    assert out["entry"]["state"] == "promoted"
    # promoted entries are user-owned now: learning no longer applies them
    assert L.apply("on Omashi")[0] == "on Omashi"


def test_promote_default_path_writes_user_lists(tmp_path):
    from voxtype_tui import sidecar as sc_mod
    cfg = tmp_path / "config.toml"
    cfg.write_text('[hotkey]\nkey = "SCROLLLOCK"\n[audio]\ndevice = "default"\n'
                   'sample_rate = 16000\nmax_duration_secs = 60\n[whisper]\nmodel = "base.en"\n'
                   '[output]\nmode = "type"\n')
    scp = tmp_path / "metadata.json"
    a = L.submit_correction(rec("I read Austin yesterday"), "I read Austen yesterday", "fix")["applied"][0]
    import voxtype_tui.config as config_mod
    orig = config_mod.safe_save
    config_mod.safe_save = lambda doc, path: path.write_text(__import__("tomlkit").dumps(doc))
    try:
        out = L.promote(a["id"], config_path=cfg, sidecar_path=scp)
    finally:
        config_mod.safe_save = orig
    assert out["added"]["vocab"] == ["Austen"]
    assert [v.phrase for v in sc_mod.load(scp).vocabulary] == ["Austen"]
    assert 'initial_prompt = "Austen"' in cfg.read_text()


def test_list_counts_and_ranking():
    L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")
    L.submit_correction(rec("I read Austin yesterday", "r2"), "I read Austen yesterday", "atspi")
    out = L.list_entries()
    assert out["counts"]["active"] == 1 and out["counts"]["suggested"] == 1
    assert out["enabled"] is True and out["everywhere"] is False
    assert all("score" in e and "seen" not in e for e in out["entries"])


def test_record_hits_off_critical_path():
    a = L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")["applied"][0]
    _, hits = L.apply("Omashi Omashi")
    L.record_hits(hits)
    e = L.list_entries()["entries"][0]
    assert e["hits"] == 2 and e["id"] == a["id"]


def test_submit_by_record_id_from_history_file():
    p = L.history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"id": "20261106T203553Z-1a2b", "final": "on Omashi", "raw": "on Omashi"}) + "\n")
    out = L.submit_correction("20261106T203553Z-1a2b", "on Omarchy", "fix")
    assert out["applied"][0]["records"] == ["20261106T203553Z-1a2b"]
    assert L.submit_correction("missing", "x", "fix")["ok"] is False


def test_replace_only_from_explicit_fix():
    with pytest.raises(ValueError):
        L.submit_correction(rec("a b"), "a c", "atspi", kind="replace")


def test_concurrent_submits_do_not_lose_entries():
    import threading
    words = ["Zorbax", "Quillon", "Vextara", "Brombly", "Kandrix", "Plorvin"]
    def go(i, w):
        heard = w[:-1] + ("o" if w[-1] != "o" else "e")
        L.submit_correction(rec(f"we use {heard} here", f"c{i}"), f"we use {w} here", "fix")
    ts = [threading.Thread(target=go, args=(i, w)) for i, w in enumerate(words)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(e["written"] for e in L.list_entries()["entries"]) == sorted(words)


def test_notification_text_and_disabled_notify(tmp_path, monkeypatch):
    assert L.notification_text([{"written": "Omarchy"}]) == \
        ("Vocabulary learned", 'Added "Omarchy" from your correction.')
    features(tmp_path, notify=False)
    called = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: called.append(a))
    assert L.notify_learned([{"id": "x", "written": "Omarchy"}]) is False
    assert called == []


# ---------------------------------------------------------------- review r1 regressions

def test_r1_english_heard_form_never_becomes_alias_after_merge():
    L.submit_correction(rec("I read Austin yesterday"), "I read Austen yesterday", "fix")
    L.submit_correction(rec("I read Auston yesterday", "r2"), "I read Austen yesterday", "fix")
    assert L.apply("I flew to Austin today")[0] == "I flew to Austin today"
    assert L.apply("I read Auston today")[0] == "I read Austen today"


def test_r1_no_wordlist_passive_never_creates_alias(monkeypatch):
    monkeypatch.setattr(vocab_corrector, "_words", None)
    out = L.submit_correction(rec("I read Austin yesterday"), "I read Austen yesterday", "atspi")
    e = out["applied"][0]
    assert e["kind"] == "spelling" and e["state"] == "suggested" and e["aliases"] == []


def test_r1_cold_apply_does_not_load_the_wordlist(monkeypatch):
    L.submit_correction(rec("I read Austin yesterday"), "I read Austen yesterday", "fix")
    monkeypatch.setattr(vocab_corrector, "_words", vocab_corrector._UNLOADED)
    if not L._WORDS_FILE.exists():
        pytest.skip("no cracklib wordlist on this machine")
    t = time.perf_counter()
    out = L.apply("then we went to Austun for the option")[0]
    ms = (time.perf_counter() - t) * 1000
    assert vocab_corrector._words is vocab_corrector._UNLOADED
    assert out == "then we went to Austen for the option"
    assert ms < 50, ms  # fresh-process cold numbers are in docs/auto-learn.md


def test_r1_english_word_bisect_matches_wordlist():
    if not L._WORDS_FILE.exists():
        pytest.skip("no cracklib wordlist")
    import voxtype_tui.vocab_corrector as vc
    saved = vc._words
    try:
        vc._words = vc._UNLOADED
        assert L._english_word("austin") and L._english_word("Option")
        assert not L._english_word("omashi") and not L._english_word("zzzzzzzzq")
        assert L._english_word("007bond")  # first line of the file
    finally:
        vc._words = saved


def test_r1_same_letters_spelling_applies():
    L.submit_correction(rec("we use code mux daily"), "we use Codemux daily", "fix")
    assert L.apply("ship code mux now")[0] == "ship Codemux now"
    assert L.apply("ship codemux now")[0] == "ship Codemux now"


def test_r1_fail_open_returns_original_input():
    L.compiled_path().parent.mkdir(parents=True, exist_ok=True)
    L.compiled_path().write_text(json.dumps({"aliases": {"omashi": ["Omarchy", "l_x"]},
                                             "max_n": 1, "terms": [[None, 1]]}))
    assert L.apply("on Omashi") == ("on Omashi", [])


@pytest.mark.parametrize("text", ["see https://omashi.com now", "cd /home/omashi", "omashi_config",
                                  "open omashi.toml", "mail me@omashi", "a #omashi tag"])
def test_r1_aliases_leave_urls_paths_identifiers(text):
    L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")
    assert L.apply(text)[0] == text


def test_r1_unicode_protected_offsets():
    L.submit_correction(rec("on Omashi"), "on Omarchy", "fix")
    text = "\u0130\u0130\u0130 Omashi Labs and Omashi"
    out = L.apply(text, user_phrases=["Omashi Labs"])[0]
    assert out == "\u0130\u0130\u0130 Omashi Labs and Omarchy"


def test_import_does_not_pull_write_only_modules():
    import subprocess, sys
    code = ("import sys, voxtype_tui.cli_postprocess; before=set(sys.modules); "
            "import voxtype_tui.learned; print(sorted(set(sys.modules)-before))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert "tempfile" not in out and "secrets" not in out, out
