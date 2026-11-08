"""AT-SPI correction watcher: learn from a fix you make right after dictating.

Started detached by the post-output hook (:func:`spawn`) right after Voxtype
delivered a dictation. It then:

1. reads the active window (``hyprctl activewindow -j``) and refuses
   password managers;
2. finds that window's process on the accessibility bus and the focused,
   editable text field in it (password fields are refused);
3. confirms the delivered text is in that field (in clipboard mode it waits
   up to the window length for you to paste it);
4. follows only the inserted span while you edit it, driven by
   ``object:text-changed`` events plus a light poll;
5. ends on Return (a newline after the span, or the field being sent and
   cleared), when the field loses focus, or after 30 s, and hands the
   edited span to :func:`learned.submit_correction` with source ``atspi``.

It costs nothing while idle: it only exists for one correction window.
A newer dictation replaces a running watcher (SIGTERM): it then ends at
once and learns nothing from that window, because the newer text may
already sit right after the span. Every failure just exits.
Terminals expose no text over AT-SPI and are skipped up front; use History
> Fix there.

Run by hand (pre-integration / debugging)::

    python3 -m voxtype_tui.learn_watch --text "what was typed" [--id REC] [-v]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

WINDOW_S = 30.0
POLL_MS = 250
FIND_RETRY_S = 2.0
MAX_NODES = 4000
MAX_TEXT = 200_000
UNDO_WAIT_S = 10.0

# Windows whose text is not on the accessibility bus: don't even start.
NO_TEXT_CLASSES = frozenset({
    "foot", "footclient", "alacritty", "kitty", "com.mitchellh.ghostty",
    "ghostty", "org.wezfurlong.wezterm", "wezterm", "xterm", "st", "urxvt",
})

REPLACED = "replaced by a newer dictation"

_log_path: Path | None = None
_verbose = False
_term = {"requested": False}


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "voxtype-tui"


def log(msg: str) -> None:
    if _verbose:
        sys.stderr.write(msg + "\n")
    if _log_path is None:
        return
    with contextlib.suppress(OSError):
        if _log_path.exists() and _log_path.stat().st_size > 256_000:
            _log_path.unlink()
        fd = os.open(str(_log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")


def _debug_text() -> bool:
    return os.environ.get("VOXTYPE_TUI_LEARN_DEBUG") == "1"


def _t(s: str) -> str:
    """Text for the log: content only in debug mode, else its length."""
    return repr(s) if _debug_text() else f"<{len(s)} chars>"


# ---------------------------------------------------------------- spawn

def spawn(record_id: str | None = None, text: str | None = None) -> bool:
    """Start a detached watcher (for the post-output hook). Returns at once;
    never raises. Skipped when learning is off or AT-SPI isn't installed."""
    try:
        from . import learned
        if not learned.is_enabled():
            return False
        import importlib.util
        if importlib.util.find_spec("gi") is None:
            return False
        argv = [sys.executable, "-m", "voxtype_tui.learn_watch"]
        if record_id:
            argv += ["--id", record_id]
        if text is not None:
            argv += ["--text", text]
        subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- single instance

def _on_sigterm(_signum, _frame) -> None:
    _term["requested"] = True


def _replaced() -> bool:
    return bool(_term["requested"])


def _hold_sigterm():
    """From now on SIGTERM (a newer dictation's watcher) only asks this
    watcher to end: every wait loop checks :func:`_replaced` and returns
    normally, so callers' cleanup runs. Returns the previous handler."""
    _term["requested"] = False
    try:
        return signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # not the main thread: keep the default
        return None


def _restore_sigterm(prev) -> None:
    if prev is None:
        return
    with contextlib.suppress(ValueError, TypeError):
        if signal.getsignal(signal.SIGTERM) is _on_sigterm:
            signal.signal(signal.SIGTERM, prev)


def _claim_slot() -> None:
    """Replace any running watcher: it finishes its window now. Call
    :func:`_hold_sigterm` first, so a newer watcher can't kill this one."""
    d = _runtime_dir()
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    pidf = d / "learn-watch.pid"
    with contextlib.suppress(OSError, ValueError):
        old = int(pidf.read_text().strip())
        if old != os.getpid() and _is_watcher(old):
            os.kill(old, signal.SIGTERM)
    tmp = pidf.with_suffix(".tmp")
    tmp.write_text(str(os.getpid()))
    os.replace(tmp, pidf)


def _release_slot() -> None:
    pidf = _runtime_dir() / "learn-watch.pid"
    with contextlib.suppress(OSError, ValueError):
        if int(pidf.read_text().strip()) == os.getpid():
            pidf.unlink()


def _is_watcher(pid: int) -> bool:
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    # standalone watcher, or the post-output hook process running one
    return b"voxtype_tui.learn_watch" in cmd or b"voxtype-tui-hook" in cmd


# ---------------------------------------------------------------- window

def _hypr_socket_window() -> dict | None:
    """Active window over Hyprland's request socket (~1 ms, no subprocess)."""
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if not sig or not rt:
        return None
    import socket

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            s.connect(os.path.join(rt, "hypr", sig, ".socket.sock"))
            s.sendall(b"j/activewindow")
            chunks = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
        w = json.loads(b"".join(chunks) or b"null")
    except (OSError, ValueError):
        return None
    return w if isinstance(w, dict) and w.get("pid") else None


def active_window() -> dict | None:
    w = _hypr_socket_window()
    if w is not None:
        return w
    try:
        p = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True,
                           text=True, timeout=2, check=False)
        data = json.loads(p.stdout or "null")
        return data if isinstance(data, dict) and data.get("pid") else None
    except Exception:
        return None


def _same_window(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    if a.get("address") and b.get("address"):
        return a["address"] == b["address"]
    return a.get("pid") == b.get("pid")


def _pid_tree(root: int) -> set[int]:
    """root plus all its descendants (renderers, helpers)."""
    children: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        with contextlib.suppress(OSError, ValueError, IndexError):
            stat = Path(f"/proc/{d}/stat").read_text()
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
            children.setdefault(ppid, []).append(int(d))
    out, todo = set(), [root]
    while todo:
        p = todo.pop()
        if p in out:
            continue
        out.add(p)
        todo.extend(children.get(p, []))
    return out


# ---------------------------------------------------------------- AT-SPI

def _atspi():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi  # type: ignore
    return Atspi


def find_apps(Atspi, pids: set[int]) -> list:
    desk = Atspi.get_desktop(0)
    apps = []
    for i in range(desk.get_child_count()):
        with contextlib.suppress(Exception):
            app = desk.get_child_at_index(i)
            if app is not None and app.get_process_id() in pids:
                apps.append(app)
    return apps


def _states(Atspi, node) -> set:
    try:
        ss = node.get_state_set()
        return {s for s in (Atspi.StateType.FOCUSED, Atspi.StateType.EDITABLE,
                            Atspi.StateType.SHOWING, Atspi.StateType.ACTIVE,
                            Atspi.StateType.DEFUNCT) if ss.contains(s)}
    except Exception:
        return {Atspi.StateType.DEFUNCT}


def find_focused_text(Atspi, apps: list):
    """BFS over showing nodes for the focused editable text field."""
    seen = 0
    queue = list(apps)
    while queue and seen < MAX_NODES:
        node = queue.pop(0)
        seen += 1
        try:
            st = _states(Atspi, node)
            if Atspi.StateType.FOCUSED in st and Atspi.StateType.EDITABLE in st:
                if node.get_text_iface() is not None:
                    return node
            n = node.get_child_count()
        except Exception:
            continue
        for i in range(min(n, 500)):
            with contextlib.suppress(Exception):
                c = node.get_child_at_index(i)
                if c is None:
                    continue
                if node in apps or Atspi.StateType.SHOWING in _states(Atspi, c):
                    queue.append(c)
    return None


def is_password(Atspi, node) -> bool:
    try:
        return node.get_role() == Atspi.Role.PASSWORD_TEXT
    except Exception:
        return True


def read_text(node) -> str | None:
    """Full text of the field (``Atspi.Text`` functions; the gi override's
    ``get_text_iface()`` returns the Accessible itself)."""
    try:
        Atspi = _atspi()
        n = Atspi.Text.get_character_count(node)
        if n > MAX_TEXT:
            return None
        return Atspi.Text.get_text(node, 0, n)
    except Exception:
        return None


def caret(node) -> int:
    try:
        return int(_atspi().Text.get_caret_offset(node))
    except Exception:
        return -1


# ---------------------------------------------------------------- span tracking

def locate(full: str, needle: str, caret_at: int) -> int:
    """Start of the occurrence of ``needle`` ending closest to the caret."""
    best, best_d = -1, None
    i = full.find(needle)
    while i >= 0:
        d = abs((i + len(needle)) - caret_at) if caret_at >= 0 else -i
        if best_d is None or d < best_d:
            best, best_d = i, d
        i = full.find(needle, i + 1)
    return best


def _wordish(ch: str) -> bool:
    return ch.isalnum() or ch in "'\u2019-_"


class Span:
    """The dictated span inside a field, kept up to date as the text changes.

    Text typed right at its end joins it only when it is part of fixing the
    span: Return, finishing its last word (``Omarch`` + ``y``), or retyping
    after you deleted back into it. Anything else typed after it (new words,
    or a newer dictation glued to ``simple.``) is outside the span."""

    def __init__(self, full: str, start: int, end: int):
        self.prev = full
        self.start = start
        self.end = end
        self.broken = False
        self.retyping = False  # an edit reached the span's end: retyped text belongs to it

    def _joins(self, inserted: str) -> bool:
        if not inserted:
            return False
        if inserted[0] == "\n" or self.retyping:
            return True
        last = self.prev[self.end - 1] if self.end > self.start else ""
        return bool(last) and _wordish(last) and _wordish(inserted[0])

    def update(self, cur: str) -> None:
        prev = self.prev
        if cur == prev or self.broken:
            return
        p = 0
        m = min(len(prev), len(cur))
        while p < m and prev[p] == cur[p]:
            p += 1
        s = 0
        while s < m - p and prev[len(prev) - 1 - s] == cur[len(cur) - 1 - s]:
            s += 1
        old_a, old_b = p, len(prev) - s  # changed range in prev
        delta = len(cur) - len(prev)
        if old_a == old_b == self.end:
            if self._joins(cur[p:len(cur) - s]):
                self.end += delta      # typing right after it, as part of the fix
        elif old_a >= self.start and old_b <= self.end:
            if old_b == self.end and old_b > old_a:
                self.retyping = True   # deleted or replaced up to the end
            self.end += delta          # edit inside the span
        elif old_b <= self.start:
            self.start += delta        # edit before it: shift
            self.end += delta
        elif old_a >= self.end:
            pass                       # edit after it
        else:
            self.broken = True         # an edit straddling the span edge
        self.prev = cur

    def text(self) -> str:
        return self.prev[self.start:self.end]


# ---------------------------------------------------------------- watch

def watch(final: str, *, window: dict | None, record: dict | None, window_s: float = WINDOW_S,
          submit: bool = True) -> dict:
    """Run one correction window. Returns a small result dict (for tests/E2E)."""
    from . import learned

    res: dict = {"ok": False}
    needle = final.strip()
    if not needle:
        return {**res, "reason": "no text"}
    if window is None:
        return {**res, "reason": "no active window"}
    cls = str(window.get("class") or "")
    if learned.is_password_manager(cls) or learned.is_password_manager(window.get("title")):
        return {**res, "reason": "password manager"}
    if cls.lower() in NO_TEXT_CLASSES:
        return {**res, "reason": f"{cls} exposes no text (use History > Fix)"}
    delivered = (record or {}).get("delivery_window")
    if isinstance(delivered, dict) and not _same_window(delivered, window):
        return {**res, "reason": "the window changed before the watcher started"}
    try:
        Atspi = _atspi()
        from gi.repository import GLib  # type: ignore
    except Exception as e:
        return {**res, "reason": f"AT-SPI unavailable: {e}"}

    deadline = time.monotonic() + window_s
    find_until = min(deadline, time.monotonic() + FIND_RETRY_S)
    pids = _pid_tree(int(window["pid"]))
    while True:
        if _replaced():
            return {**res, "reason": REPLACED}
        apps = find_apps(Atspi, pids)
        node = find_focused_text(Atspi, apps) if apps else None
        if node is not None or time.monotonic() >= find_until:
            break
        time.sleep(0.2)
    if _replaced():
        return {**res, "reason": REPLACED}
    if node is None:
        return {**res, "reason": f"no focused text field on the accessibility bus ({cls})"}
    if is_password(Atspi, node):
        return {**res, "reason": "password field"}
    if not _same_window(window, active_window()):
        return {**res, "reason": "the window changed before the watcher started"}
    log(f"field found in {cls} role={node.get_role_name()}")

    # Wait for the dictated text to be in the field (clipboard mode: the
    # user pastes it themselves).
    span = None
    while time.monotonic() < deadline:
        if _replaced():
            log(f"end: {REPLACED} (before the dictated text appeared)")
            return {**res, "reason": REPLACED}
        st0 = _states(Atspi, node)
        if Atspi.StateType.DEFUNCT in st0 or Atspi.StateType.FOCUSED not in st0:
            return {**res, "reason": "focus left before the dictated text appeared"}
        full = read_text(node)
        if full is None:
            return {**res, "reason": "field unreadable"}
        start = locate(full, needle, caret(node))
        if start >= 0:
            span = Span(full, start, start + len(needle))
            break
        time.sleep(POLL_MS / 1000)
    if span is None:
        return {**res, "reason": "dictated text never appeared in the field"}
    log(f"armed: span {span.start}..{span.end} {_t(needle)}")

    state = {"last_good": needle, "end": None}
    loop = GLib.MainLoop()
    app_pid = None
    with contextlib.suppress(Exception):
        app_pid = node.get_process_id()

    def sample(_why: str) -> None:
        if state["end"]:
            return
        if _replaced():
            # Checked before reading: the newer text may already be in the field.
            finish(REPLACED)
            return
        st = _states(Atspi, node)
        if Atspi.StateType.DEFUNCT in st:
            finish("field closed")
            return
        cur = read_text(node)
        if cur is None:
            finish("field unreadable")
            return
        span.update(cur)
        if span.broken:
            finish("edit straddles the dictation; ignored", broken=True)
            return
        edited = span.text()
        if edited.count("\n") > needle.count("\n"):
            # Return ends the window; the newline itself is not part of the fix.
            state["last_good"] = edited.replace("\n", " ", 1)
            finish("return")
            return
        if cur.strip() == "" and needle:
            finish("field cleared (sent)")
            return
        state["last_good"] = edited
        if Atspi.StateType.FOCUSED not in st:
            finish("focus left")

    def finish(why: str, broken: bool = False) -> None:
        if state["end"]:
            return
        state["end"] = why
        state["broken"] = broken
        loop.quit()

    def on_event(ev) -> None:
        with contextlib.suppress(Exception):
            if app_pid is None or ev.source.get_process_id() == app_pid:
                sample("event")

    listener = None
    with contextlib.suppress(Exception):
        listener = Atspi.EventListener.new(on_event)
        listener.register("object:text-changed")
        listener.register("object:state-changed:focused")

    def tick() -> bool:
        if _replaced():
            finish(REPLACED)
            return False
        if time.monotonic() >= deadline:
            finish("timeout")
            return False
        sample("poll")
        return not state["end"]

    # SIGTERM is handled by the Python handler installed at slot claim
    # (_hold_sigterm) for the watcher's whole life; the poll sees it.
    GLib.timeout_add(POLL_MS, tick)
    loop.run()
    with contextlib.suppress(Exception):
        if listener is not None:
            listener.deregister("object:text-changed")
            listener.deregister("object:state-changed:focused")

    edited = state["last_good"]
    res = {"ok": True, "end": state["end"], "edited": edited if _debug_text() else len(edited),
           "changed": edited.strip() != needle}
    log(f"end: {state['end']} edited={_t(edited)}")
    if state["end"] == REPLACED:
        # The newer dictation may be glued to the span: never learn from it.
        # History > Fix still works for this dictation.
        return res
    if state.get("broken") or edited.strip() == needle or not submit:
        return res
    rec = dict(record or {})
    rec.setdefault("final", final)
    rec.setdefault("app", {"class": cls, "title": window.get("title"), "pid": window.get("pid"),
                           "address": window.get("address")})
    out = learned.submit_correction(rec, edited.strip(), "atspi")
    res["learned"] = [{"id": e["id"], "written": e["written"], "state": e["state"]}
                      for e in out.get("applied", [])]
    res["skipped"] = out.get("skipped", [])
    res["reason"] = out.get("reason")
    log(f"submit: learned={[e['id'] for e in res['learned']]} skipped={len(res['skipped'])} "
        f"reason={res['reason'] if _debug_text() else bool(res['reason'])}")
    # Every transition into active (first sighting, second sighting, a new
    # confirmed alias) gets the notification and its Undo.
    fresh, ids = [], set()
    for e in out.get("applied", []):
        if e.get("activated") and e["id"] not in ids:
            ids.add(e["id"])
            fresh.append(e)
    if fresh:
        undone = learned.notify_learned(fresh, record_id=rec.get("id"),
                                        wait_for_undo_s=UNDO_WAIT_S)
        res["undone"] = undone
        log(f"notified; undo={undone}")
    return res


def _record_text(record_id: str | None) -> dict | None:
    from . import learned
    if not record_id:
        return None
    for _ in range(10):  # the record may land a moment after post-output
        if _replaced():
            return None
        rec = learned.find_record(record_id)
        if rec:
            return rec
        time.sleep(0.1)
    return None


def _latest_record() -> dict | None:
    """Newest history record (when no id was passed)."""
    from . import learned
    try:
        with open(learned.history_path(), "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        with contextlib.suppress(ValueError):
            rec = json.loads(line)
            if isinstance(rec, dict) and isinstance(rec.get("final"), str):
                return rec
    return None


def on_post_output(record: dict, window: dict | None = None) -> dict | None:
    """Entry point for the post-output hook, which already runs in its own
    detached process: watch this record's correction window in-process.

    ``window`` is the active window captured by the hook right when it
    started (the delivery window); without it the window is read now, which
    is later when the hook did other work first. Never raises."""
    global _log_path
    prev_term = None
    try:
        if window is None:
            window = active_window()  # the window the text was just delivered to
        from . import learned
        if not isinstance(record, dict) or not learned.is_enabled():
            return None
        final = record.get("final")
        if not isinstance(final, str):
            return None
        _runtime_dir().mkdir(parents=True, exist_ok=True)
        _log_path = _runtime_dir() / "learn-watch.log"
        prev_term = _hold_sigterm()
        _claim_slot()
        rec = dict(record)
        if window is not None:
            rec["delivery_window"] = {k: window.get(k) for k in ("address", "pid", "class")}
            rec["app"] = {k: window.get(k) for k in ("class", "title", "pid", "address")}
        res = watch(final, window=active_window(), record=rec)
        log("result " + json.dumps(_safe_result(res)))
        return res
    except Exception as e:
        log(f"error: {type(e).__name__}: {e}")
        return None
    finally:
        _release_slot()
        _restore_sigterm(prev_term)


def _safe_result(res: dict) -> dict:
    if _debug_text():
        return res
    out = {k: v for k, v in res.items() if k in ("ok", "end", "changed", "undone")}
    out["learned"] = len(res.get("learned") or [])
    out["reason"] = res.get("reason") is not None
    return out


def main(argv: list[str] | None = None) -> int:
    global _log_path, _verbose
    ap = argparse.ArgumentParser(prog="voxtype_tui.learn_watch")
    ap.add_argument("--id")
    ap.add_argument("--text")
    ap.add_argument("--window", type=float, default=WINDOW_S)
    ap.add_argument("--no-submit", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    _verbose = a.verbose
    prev_term = None
    try:
        from . import learned
        if not learned.is_enabled():
            return 0
        _runtime_dir().mkdir(parents=True, exist_ok=True)
        _log_path = _runtime_dir() / "learn-watch.log"
        prev_term = _hold_sigterm()
        _claim_slot()
        rec = _record_text(a.id) if a.id else None
        if rec is None and a.text is None:
            rec = _latest_record()
        final = a.text if a.text is not None else (rec or {}).get("final")
        if not isinstance(final, str):
            log("no record")
            return 0
        res = watch(final, window=active_window(), record=rec, window_s=min(a.window, WINDOW_S),
                    submit=not a.no_submit)
        log("result " + json.dumps(_safe_result(res)))
        if a.verbose:
            print(json.dumps(res))
        return 0
    except Exception as e:  # never surface anything to the user
        log(f"error: {type(e).__name__}: {e}")
        return 0
    finally:
        _release_slot()
        _restore_sigterm(prev_term)


if __name__ == "__main__":
    sys.exit(main())
