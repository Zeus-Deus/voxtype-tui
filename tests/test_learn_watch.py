"""AT-SPI watcher logic that runs without a desktop: span tracking, refusals,
spawn gating. The real-app path is proven in the VM E2E (docs/auto-learn.md)."""

from __future__ import annotations

import json

import pytest

from voxtype_tui import learn_watch as W


def test_span_tracks_edit_inside():
    full = "Hello. the option on Omashi. Bye"
    s = W.Span(full, 7, 28)
    assert s.text() == "the option on Omashi."
    cur = full.replace("Omashi", "Omarch")
    s.update(cur)
    cur = cur.replace("Omarch", "Omarchy")
    s.update(cur)
    assert s.text() == "the option on Omarchy."
    assert not s.broken


def test_span_shifts_on_edit_before_and_ignores_after():
    full = "AB the option on Omashi. CD"
    s = W.Span(full, 3, 24)
    s.update("XXAB the option on Omashi. CD")
    assert s.text() == "the option on Omashi."
    s.update("XXAB the option on Omashi. CDEF")
    assert s.text() == "the option on Omashi."


def test_span_includes_typing_right_after():
    s = W.Span("on Omashi", 0, 9)
    s.update("on Omashi and more")
    assert s.text() == "on Omashi and more"


def test_span_straddling_edit_is_broken():
    s = W.Span("AB on Omashi", 3, 12)
    s.update("A on Omashi")  # one edit deleting across the span start
    s.update("A")
    assert s.broken


def test_locate_prefers_occurrence_at_caret():
    full = "hi there. hi there."
    assert W.locate(full, "hi there.", len(full)) == 10
    assert W.locate(full, "hi there.", 9) == 0
    assert W.locate(full, "nope", 3) == -1


@pytest.mark.parametrize("cls,why", [("foot", "exposes no text"), ("org.keepassxc.KeePassXC", "password"),
                                     ("Bitwarden", "password")])
def test_refusals_before_touching_atspi(cls, why, monkeypatch):
    monkeypatch.setattr(W, "_atspi", lambda: (_ for _ in ()).throw(AssertionError("no a11y")))
    res = W.watch("some text", window={"class": cls, "pid": 1}, record=None)
    assert res["ok"] is False and why in res["reason"]


def test_spawn_skipped_when_learning_off(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "voxtype-tui").mkdir()
    (tmp_path / "voxtype-tui" / "features.json").write_text(json.dumps({"learning": {"enabled": False}}))
    called = []
    monkeypatch.setattr(W.subprocess, "Popen", lambda *a, **k: called.append(a))
    assert W.spawn("x") is False and called == []


def test_spawn_detaches(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    seen = {}
    monkeypatch.setattr(W.subprocess, "Popen", lambda argv, **k: seen.update(argv=argv, **k))
    assert W.spawn("rec-1") is True
    assert seen["start_new_session"] is True and seen["close_fds"] is True
    assert seen["argv"][-2:] == ["--id", "rec-1"]


def test_r1_window_must_match_the_dictation(monkeypatch):
    monkeypatch.setattr(W, "_atspi", lambda: (_ for _ in ()).throw(AssertionError("no a11y")))
    res = W.watch("text", window={"class": "org.gnome.TextEditor", "pid": 5, "address": "0xb"},
                  record={"id": "r", "final": "text", "app": {"address": "0xa", "pid": 5}})
    assert res["ok"] is False and "window changed" in res["reason"]
