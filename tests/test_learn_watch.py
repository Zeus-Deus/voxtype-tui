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
                  record={"id": "r", "final": "text", "delivery_window": {"address": "0xa", "pid": 5}})
    assert res["ok"] is False and "window changed" in res["reason"]


def test_on_post_output_never_raises_and_respects_off(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "voxtype-tui").mkdir()
    (tmp_path / "voxtype-tui" / "features.json").write_text(json.dumps({"learning": {"enabled": False}}))
    monkeypatch.setattr(W, "active_window", lambda: {"class": "x", "pid": 1})
    assert W.on_post_output({"final": "hi"}) is None
    assert W.on_post_output(None) is None  # type: ignore[arg-type]


def test_on_post_output_binds_delivery_window(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(W, "active_window", lambda: {"class": "foot", "pid": 7, "address": "0x1"})
    seen = {}
    monkeypatch.setattr(W, "watch", lambda final, window, record: seen.update(r=record) or {"ok": False})
    W.on_post_output({"id": "r1", "final": "hello", "app": {"class": "old", "address": "0x9"}})
    assert seen["r"]["delivery_window"] == {"address": "0x1", "pid": 7, "class": "foot"}
    assert seen["r"]["app"]["class"] == "foot"


def test_r2_hook_window_is_the_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(W, "active_window", lambda: {"class": "B", "pid": 8, "address": "0xb"})
    seen = {}
    monkeypatch.setattr(W, "watch", lambda final, window, record: seen.update(w=window, r=record) or {"ok": False})
    W.on_post_output({"id": "r1", "final": "hello"}, window={"class": "A", "pid": 7, "address": "0xa"})
    assert seen["r"]["delivery_window"]["address"] == "0xa"   # bound to the delivery window
    assert seen["w"]["address"] == "0xb"   # watch() gets the current window and refuses (r1 test)


# ---------------------------------------------------------------- notify on activation

def _learning_home(tmp_path, monkeypatch):
    from pathlib import Path
    from voxtype_tui import cli_postprocess, vocab_corrector
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(cli_postprocess, "SIDECAR_PATH", Path(tmp_path / "config" / "voxtype-tui" / "metadata.json"))
    monkeypatch.setattr(vocab_corrector, "_words", frozenset("i read austin yesterday on the".split()))


def _drive_watch(monkeypatch, final, edited, rid):
    """Run the real watch() against a fake text field: the dictated text
    appears, the user edits it, then focus leaves."""
    import sys
    import types

    class StateType:
        DEFUNCT, FOCUSED = "defunct", "focused"

    class FakeAtspi:
        pass
    FakeAtspi.StateType = StateType

    class Node:
        def get_role_name(self):
            return "text"

        def get_process_id(self):
            return 5

    class Loop:
        def __init__(self):
            self.done = False

        def run(self):
            while not self.done and calls:
                if calls[0]() is False:
                    break

        def quit(self):
            self.done = True
    calls = []
    glib = types.SimpleNamespace(MainLoop=Loop, PRIORITY_HIGH=0,
                                 timeout_add=lambda ms, fn: calls.append(fn),
                                 unix_signal_add=lambda *a: None)
    repo = types.ModuleType("gi.repository")
    repo.GLib = glib
    gi = types.ModuleType("gi")
    gi.repository = repo
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)
    reads = {"n": 0}

    def read_text(node):
        reads["n"] += 1
        return final if reads["n"] == 1 else edited

    def states(Atspi, node):
        return {StateType.FOCUSED} if reads["n"] < 3 else set()
    window = {"class": "org.gnome.TextEditor", "pid": 5, "address": "0xa"}
    monkeypatch.setattr(W, "_atspi", lambda: FakeAtspi)
    monkeypatch.setattr(W, "_pid_tree", lambda pid: {pid})
    monkeypatch.setattr(W, "find_apps", lambda Atspi, pids: [object()])
    monkeypatch.setattr(W, "find_focused_text", lambda Atspi, apps: Node())
    monkeypatch.setattr(W, "is_password", lambda Atspi, node: False)
    monkeypatch.setattr(W, "active_window", lambda: window)
    monkeypatch.setattr(W, "_states", states)
    monkeypatch.setattr(W, "read_text", read_text)
    monkeypatch.setattr(W, "caret", lambda node: 0)
    return W.watch(final, window=window, record={"id": rid, "final": final}, window_s=5)


def test_second_sighting_activation_notifies_with_undo(tmp_path, monkeypatch):
    from voxtype_tui import learned
    _learning_home(tmp_path, monkeypatch)
    notified = []
    monkeypatch.setattr(learned, "notify_learned",
                        lambda entries, record_id=None, wait_for_undo_s=0:
                        notified.append(([e["written"] for e in entries], record_id, wait_for_undo_s)))
    o, e = "I read Austin yesterday", "I read Austen yesterday"
    first = _drive_watch(monkeypatch, o, e, "w1")
    assert first["learned"][0]["state"] == "suggested" and notified == []
    second = _drive_watch(monkeypatch, o, e, "w2")
    assert second["learned"][0]["state"] == "active"
    assert notified == [(["Austen"], "w2", W.UNDO_WAIT_S)]


def test_repeat_on_an_already_active_entry_does_not_notify(tmp_path, monkeypatch):
    from voxtype_tui import learned
    _learning_home(tmp_path, monkeypatch)
    notified = []
    monkeypatch.setattr(learned, "notify_learned", lambda entries, **k: notified.append(entries))
    o, e = "on the Omashi", "on the Omarchy"
    _drive_watch(monkeypatch, o, e, "w1")   # unambiguous: active at once, notified
    _drive_watch(monkeypatch, o, e, "w2")   # nothing new became active
    assert len(notified) == 1
