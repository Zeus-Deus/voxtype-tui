"""Child process for tests/test_learn_watch.py: runs the real
``learn_watch.on_post_output`` against a fake text field, so the test can
send it a real SIGTERM (as a newer dictation's watcher does).

Usage: python _learn_watch_child.py <scenario> <workdir>

* ``prearm``: the dictated text never shows up, so the watcher stays in
  its wait-for-the-text loop.
* ``armed``: the dictated text shows up, then a newer dictation is typed
  right after it (no space) before the SIGTERM arrives.

Writes ``<workdir>/ready`` once the scenario's point is reached and
``<workdir>/out.json`` from a ``finally`` (like the post-output hook's
cleanup), with the watcher's result.
"""

from __future__ import annotations

import json
import signal
import sys
import time
import types
from pathlib import Path

scenario, work = sys.argv[1], Path(sys.argv[2])


class StateType:
    DEFUNCT, FOCUSED = "defunct", "focused"


class FakeAtspi:
    StateType = StateType


class Node:
    def get_role_name(self):
        return "text"

    def get_process_id(self):
        return 5


calls: list = []


class Loop:
    def __init__(self):
        self.done = False

    def run(self):
        while not self.done and calls:
            if calls[0]() is False:
                break
            time.sleep(0.02)

    def quit(self):
        self.done = True


def unix_signal_add(_prio, sig, fn):
    # like GLib: the callback runs from the main loop when the signal arrives
    signal.signal(sig, lambda *_a: fn())


glib = types.SimpleNamespace(MainLoop=Loop, PRIORITY_HIGH=0,
                             timeout_add=lambda ms, fn: calls.append(fn),
                             unix_signal_add=unix_signal_add)
repo = types.ModuleType("gi.repository")
repo.GLib = glib
gi = types.ModuleType("gi")
gi.repository = repo
sys.modules["gi"] = gi
sys.modules["gi.repository"] = repo

from voxtype_tui import cli_postprocess, learned, vocab_corrector  # noqa: E402
from voxtype_tui import learn_watch as W  # noqa: E402

cli_postprocess.SIDECAR_PATH = work / "metadata.json"
vocab_corrector._words = frozenset("make it simple hey there".split())
learned.notify_learned = lambda *a, **k: False  # never a desktop notification

FINAL = "Make it simple."
NEWER = "Hey there."
reads = {"n": 0}


def ready():
    p = work / "ready"
    if not p.exists():
        p.write_text("1")


def read_text(_node):
    reads["n"] += 1
    if scenario == "prearm":
        ready()
        return "something else"
    if reads["n"] == 1:
        return FINAL
    ready()
    return FINAL + NEWER  # the newer dictation, typed right at the end of the span


window = {"class": "org.gnome.TextEditor", "pid": 5, "address": "0xa"}
W._atspi = lambda: FakeAtspi
W._pid_tree = lambda pid: {pid}
W.find_apps = lambda Atspi, pids: [object()]
W.find_focused_text = lambda Atspi, apps: Node()
W.is_password = lambda Atspi, node: False
W.active_window = lambda: window
W._states = lambda Atspi, node: {StateType.FOCUSED}
W.read_text = read_text
W.caret = lambda node: len(FINAL)

res = None
try:
    res = W.on_post_output({"id": "rec-1", "final": FINAL}, window=window)
finally:
    (work / "out.json").write_text(json.dumps({"res": res, "cleanup": True}))
