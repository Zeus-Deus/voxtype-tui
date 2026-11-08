"""Voxtype hook wiring + the ``voxtype-tui-hook <event>`` entry point.

Wiring
------
We only ever touch ``[output] pre_recording_command`` and
``post_output_command``, and only when the value is absent or exactly one
we wrote (the string recorded in ``features.json → managed_hooks`` or our
current command form). A user's own command is never modified: the feature
that needs that hook reports ``unavailable`` with a reason instead.

The wired string is downgrade/uninstall safe. Voxtype 1.1.1 runs hook
strings through ``sh -c`` (measured), so::

    command -v voxtype-tui-hook >/dev/null 2>&1 && exec voxtype-tui-hook pre-recording; exit 0

exits 0 and costs ~2 ms when voxtype-tui is not installed. (A missing
executable or a non-zero exit only makes voxtype log a WARN; dictation still
works — also measured.)

* ``pre_recording_command`` is wired when usage, history or archive is on:
  it timestamps the recording start (for measured durations), notes the
  focused window and, for the archive, starts a detached recorder. Voxtype
  starts capturing before it runs this hook, so it cannot cost first words.
* ``post_output_command`` is wired when usage, history or the archive is
  on (the worker drains the queue after every dictation), or the learning
  watcher exists and learning is on. Voxtype waits for this hook before it
  goes idle, so the hook forks its work into a detached process and
  returns. With the user's own post_output_command, the post-process CLI
  starts a detached drain once the queue is large (see history.enqueue).
* ``pre_output_command`` is never wired.
* ``[output.post_process]`` is wired (non-destructively, as before) when any
  capture feature or vocabulary/dictionary needs it.

Reconcile is pure (:func:`reconcile`), idempotent and retried on every
``AppState`` load/save and ``features.update`` (:func:`sync_config`).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

from . import features as F

PRE_RECORDING = "output.pre_recording_command"
POST_OUTPUT = "output.post_output_command"
PRE_OUTPUT = "output.pre_output_command"
EVENTS = ("pre-recording", "pre-output", "post-output")
EVENT_KEYS = {"pre-recording": PRE_RECORDING, "post-output": POST_OUTPUT}
HOOK_LABEL = {PRE_RECORDING: "pre_recording_command", POST_OUTPUT: "post_output_command"}


def command_for(event: str) -> str:
    return (
        "command -v voxtype-tui-hook >/dev/null 2>&1 && "
        f"exec voxtype-tui-hook {event}; exit 0"
    )


OUR_COMMANDS = {key: command_for(ev) for ev, key in EVENT_KEYS.items()}


# ---------------------------------------------------------------------------
# What the features need
# ---------------------------------------------------------------------------

def _module_exists(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def recorder_binary() -> str | None:
    import shutil

    return shutil.which("pw-record") or shutil.which("parec")


def needed(feats: dict) -> dict[str, bool]:
    archive = feats["archive"]["enabled"]
    capture = feats["usage"]["enabled"] or feats["history"]["enabled"] or archive
    learning = feats["learning"]["enabled"] and _module_exists("voxtype_tui.learn_watch")
    return {
        PRE_RECORDING: bool(capture),
        # Usage/history need it too: the detached post-output worker drains
        # the queue after every dictation, without anyone reading.
        POST_OUTPUT: bool(capture or learning),
        "post_process": bool(capture or feats["learning"]["enabled"]),
    }


def _get(doc: Any, dotted: str) -> Any:
    node = doc
    for part in dotted.split("."):
        if node is None or not hasattr(node, "get"):
            return None
        node = node.get(part)
    return node


class Plan:
    """Result of :func:`reconcile` (plain class: dataclasses costs ~8 ms of
    import time in the per-dictation hook process)."""

    def __init__(self, managed: dict[str, str] | None = None):
        self.changed = False
        self.managed: dict[str, str] = dict(managed or {})
        self.wired: list[str] = []
        self.unwired: list[str] = []
        self.foreign: dict[str, str] = {}  # hook path -> current value
        self.unavailable: dict[str, str] = {}  # feature -> reason


def availability(doc: Any, feats: dict, foreign: dict[str, str] | None = None) -> dict[str, str]:
    """feature -> short human reason, for features that cannot work now."""
    from . import config

    if foreign is None:
        foreign = {}
        for key in (PRE_RECORDING, POST_OUTPUT):
            cur = _get(doc, key)
            if cur is not None and str(cur) and str(cur) not in (
                (OUR_COMMANDS[key],) + F.owned_hook_strings(feats, key)
            ):
                foreign[key] = str(cur)
    reasons: dict[str, str] = {}
    pp = config.get_post_process(doc).get("command")
    pp_foreign = bool(pp) and not config.is_our_post_process_command(pp)
    if pp_foreign:
        why = "your own post-processing command is set"
        reasons["usage"] = why
        reasons["history"] = why
        reasons["learning"] = why
    if PRE_RECORDING in foreign:
        reasons["archive"] = "your own pre_recording_command is set"
    elif POST_OUTPUT in foreign:
        reasons["archive"] = "your own post_output_command is set"
    elif pp_foreign:
        reasons["archive"] = "your own post-processing command is set"
    elif feats["archive"]["save_audio"] and recorder_binary() is None:
        reasons["archive"] = "pw-record (PipeWire) is not installed"
    if "learning" not in reasons and not _module_exists("voxtype_tui.learned"):
        reasons["learning"] = "needs a newer voxtype-tui"
    if not _module_exists("voxtype_tui.learn_everywhere"):
        reasons["everywhere"] = "needs a newer voxtype-tui"
    elif "learning" in reasons:
        reasons["everywhere"] = reasons["learning"]
    return reasons


def reconcile(doc: Any, feats: dict) -> Plan:
    """Bring the hook commands in ``doc`` (a tomlkit document) in line with
    ``feats``. Mutates ``doc``; never touches a value we don't own."""
    from . import config, migrations

    plan = Plan(managed=dict(feats.get(F.MANAGED_HOOKS_KEY) or {}))
    want = needed(feats)
    for key in (PRE_RECORDING, POST_OUTPUT):
        ours = OUR_COMMANDS[key]
        cur = _get(doc, key)
        cur_s = None if cur is None else str(cur)
        # We only ever modify or remove a value equal to a string we
        # recorded writing (committed or pending). An unrecorded value that
        # happens to be exactly our command is adopted when wanted (no edit
        # needed) and otherwise left alone.
        owned = cur_s is not None and cur_s in F.owned_hook_strings(feats, key)
        if want[key]:
            if cur_s is None or cur_s == "" or owned:
                if cur_s != ours:
                    _set_output(doc, key, ours)
                    plan.changed = True
                    plan.wired.append(key)
                plan.managed[key] = ours
            elif cur_s == ours:
                plan.managed[key] = ours
            else:
                plan.foreign[key] = cur_s
                plan.managed.pop(key, None)
        else:
            if owned:
                _del_output(doc, key)
                plan.changed = True
                plan.unwired.append(key)
            elif cur_s is not None and cur_s not in ("", ours):
                plan.foreign[key] = cur_s
            plan.managed.pop(key, None)
    if want["post_process"]:
        if migrations._wire_postprocess(doc):
            plan.changed = True
            plan.wired.append("output.post_process.command")
    plan.unavailable = availability(doc, feats, plan.foreign)
    _ = config  # imported for availability(); keeps import cost in one place
    return plan


def _set_output(doc: Any, key: str, value: str) -> None:
    import tomlkit

    name = key.split(".", 1)[1]
    if "output" not in doc:
        doc["output"] = tomlkit.table()
    out = doc["output"]
    # Keep scalar keys ahead of [output.*] sub-tables: tomlkit appends new
    # keys at the end of the table body, which is still valid TOML.
    out[name] = value


def _del_output(doc: Any, key: str) -> None:
    name = key.split(".", 1)[1]
    out = doc.get("output")
    if out is not None and name in out:
        del out[name]


def sync_config(config_path: Path | None = None, features_file: Path | None = None) -> dict:
    """Load config.toml, reconcile, save it (voxtype-validated) when changed,
    then record ownership. Returns {changed, wired, unwired, unavailable}."""
    from . import config

    path = config_path or config.CONFIG_PATH
    feats = F.load(features_file)
    if not path.exists():
        return {"changed": False, "wired": [], "unwired": [],
                "unavailable": availability({}, feats, {})}
    doc = config.load(path)
    old = dict(feats.get(F.MANAGED_HOOKS_KEY) or {})
    plan = reconcile(doc, feats)
    record_intent(feats, plan, features_file)
    if plan.changed:
        config.safe_save(doc, path)  # on failure the pending record keeps both strings ours
    commit_ownership(plan, features_file)
    _ = old
    return {"changed": plan.changed, "wired": plan.wired, "unwired": plan.unwired,
            "unavailable": plan.unavailable}


def record_intent(feats: dict, plan: "Plan", features_file: Path | None = None) -> None:
    """Before config.toml is written: record the hook strings about to be
    ours as *pending*, keeping the committed record intact."""
    old = dict(feats.get(F.MANAGED_HOOKS_KEY) or {})
    pending = {k: list(v) for k, v in (feats.get(F.PENDING_HOOKS_KEY) or {}).items()}
    changed = False
    # Keep every string already pending (it may be installed after an
    # earlier crash or failed save) and add the ones about to be written.
    for k, v in plan.managed.items():
        if old.get(k) != v and v not in pending.setdefault(k, []):
            pending[k].append(v)
            changed = True
    pending = {k: v for k, v in pending.items() if v}
    if changed:
        F.set_managed_hooks(old, features_file, pending=pending)


def commit_ownership(plan: "Plan", features_file: Path | None = None) -> None:
    """After config.toml was written: the plan's strings are the record."""
    cur = F.load(features_file)
    if (cur.get(F.MANAGED_HOOKS_KEY) != plan.managed) or cur.get(F.PENDING_HOOKS_KEY):
        F.set_managed_hooks(plan.managed, features_file, pending={})


def unwire_all(config_path: Path | None = None, features_file: Path | None = None) -> dict:
    """Remove every hook we own (package removal). Foreign values untouched."""
    from . import config

    path = config_path or config.CONFIG_PATH
    feats = F.load(features_file)
    managed = dict(feats.get(F.MANAGED_HOOKS_KEY) or {})
    changed = False
    if path.exists():
        doc = config.load(path)
        for key in (PRE_RECORDING, POST_OUTPUT):
            cur = _get(doc, key)
            if cur is not None and str(cur) in F.owned_hook_strings(feats, key):
                _del_output(doc, key)
                changed = True
        if changed:
            config.safe_save(doc, path, validate=False)
    if managed or feats.get(F.PENDING_HOOKS_KEY):
        F.set_managed_hooks({}, features_file, pending={})
    return {"changed": changed}


# ---------------------------------------------------------------------------
# voxtype-tui-hook <event>  (runs on every dictation: stay lean, never fail)
# ---------------------------------------------------------------------------

def _hypr_active_window() -> dict | None:
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if not sig or not rt:
        return None
    import socket

    path = os.path.join(rt, "hypr", sig, ".socket.sock")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.15)
            s.connect(path)
            s.sendall(b"j/activewindow")
            chunks = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
        w = json.loads(b"".join(chunks) or b"{}")
    except (OSError, ValueError):
        return None
    if not isinstance(w, dict) or not w:
        return None
    return {"class": w.get("class"), "title": w.get("title"), "pid": w.get("pid"),
            "address": w.get("address")}


def _recorder_argv(binary: str, device: str, wav: str) -> list[str]:
    name = os.path.basename(binary)
    if name == "parec":
        argv = [binary, "--rate=16000", "--channels=1", "--format=s16le", "--file-format=wav"]
        if device and device != "default":
            argv.append(f"--device={device}")
        return argv + [wav]
    argv = [binary, "--rate", "16000", "--channels", "1", "--format", "s16",
            "--media-role", "Communication"]
    if device and device != "default":
        argv += ["--target", device]
    return argv + [wav]


def _purge_stale(rt: Path, keep: str | None = None) -> None:
    """Scratch of cancelled / no-speech recordings (no post-output ran)."""
    import stat

    now = time.time()
    from . import rtdir

    rtdir.prune(str(rt), now, 600, "handoff")  # records no worker picked up
    for sub, prefix, suffix in (("rec", "", ".wav"), ("", "rec-used-", ".json"),
                                ("", "rec-discard-", ".json")):
        d = rt / sub if sub else rt
        try:
            if not stat.S_ISDIR(os.lstat(d).st_mode):
                continue  # never follow a symlinked scratch dir
            with os.scandir(d) as it:
                for e in it:
                    if not (e.name.startswith(prefix) and e.name.endswith(suffix)):
                        continue
                    # lstat: a symlink is never followed (nor unlinked as data)
                    try:
                        if not e.is_file(follow_symlinks=False) or e.path == keep:
                            continue
                        if now - e.stat(follow_symlinks=False).st_mtime > 600:
                            os.unlink(e.path)
                    except OSError:
                        continue  # removed meanwhile: the rest still gets purged
        except OSError:
            pass


def resolve_source(device: str) -> tuple:
    """Map voxtype's ``audio.device`` to a PipeWire/Pulse source name.

    Returns ``(target, note)``: ``None`` = the default source; ``False`` =
    the configured device was not found (the archive then saves no audio
    rather than recording a different microphone). Matching order: exact
    name, case-insensitive exact, then a unique case-insensitive substring
    among capture (non-monitor) sources."""
    import subprocess

    if not device or device == "default":
        return None, "default source"
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True,
                             text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    names = [line.split("\t")[1] for line in out.splitlines() if line.count("\t") >= 1]
    if device in names:
        return device, "configured device"
    low = device.lower()
    for n in names:
        if n.lower() == low:
            return n, "configured device"
    partial = [n for n in names if low in n.lower() and not n.endswith(".monitor")]
    if len(partial) == 1:
        return partial[0], f"matched configured device {device!r}"
    if partial:
        return False, f"configured device {device!r} matches several sources"
    return False, f"configured device {device!r} not found"


def _supervise(proc, cap_s: float) -> None:
    """Detached: stop the recorder once voxtype leaves `recording` (50 ms
    polls, only while recording) or at the cap, then reap it."""
    import signal

    from . import capture

    deadline = time.time() + cap_s
    state_path = capture.state_file()
    if cap_s > 0:
        time.sleep(0.2)
    while time.time() < deadline and proc.poll() is None:
        state, _ = capture.read_state(state_path)
        if state != "recording":
            break
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGINT)  # pw-record finalizes the WAV header
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass


def on_pre_recording() -> None:
    import subprocess

    from . import capture

    feats = F.load()
    cfg = capture.config_info()
    state_path = capture.state_file(cfg)
    state, mtime = capture.read_state(state_path)
    now = time.time()
    start = mtime if state == "recording" and mtime and now - mtime < 5 else now
    info: dict[str, Any] = {"start": start, "hook_ts": now, "app": None}
    archive = feats["archive"]
    if not (feats["usage"]["enabled"] or feats["history"]["enabled"] or archive["enabled"]):
        return
    info["app"] = _hypr_active_window()
    rt = F.ensure_private_dir(F.runtime_dir())
    _purge_stale(rt)
    if archive["enabled"] and archive["save_audio"] and state_path is None:
        # Without voxtype's state file nothing tells the recorder when the
        # recording stops: save no audio rather than a wrong clip.
        from . import history

        history.note_failure("archive", "audio not saved: voxtype's state_file is disabled")
    elif archive["enabled"] and archive["save_audio"]:
        binary = recorder_binary()
        if binary:
            rec_dir = F.ensure_private_dir(rt / "rec")
            wav = str(rec_dir / f"{int(start * 1000)}.wav")
            target, note = resolve_source(str(cfg.get("device", "default")))
            if target is False:
                from . import history

                history.note_failure("archive", f"audio not saved: {note}")
                F.atomic_write_text(rt / "rec-start.json", json.dumps(info))
                return
            old_umask = os.umask(0o077)
            try:
                proc = subprocess.Popen(
                    _recorder_argv(binary, target or "default", wav),  # None = default
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                )
            finally:
                os.umask(old_umask)
            cap = float(cfg.get("max_duration_secs") or 60) + 5
            try:
                info.update({"wav": wav, "recorder_pid": proc.pid, "recorder_started": time.time(),
                             "device": cfg.get("device", "default"), "source": target or "default",
                             "source_note": note, "recorder": os.path.basename(binary)})
                F.atomic_write_text(rt / "rec-start.json", json.dumps(info))
            except BaseException:
                cap = 0  # could not hand it off: stop the recorder now
                raise
            finally:
                # Already detached (hook_main). The cap holds even when the
                # hand-off failed, so a recorder never outlives its recording.
                _supervise(proc, cap)
                if cap == 0:
                    try:
                        os.unlink(wav)
                    except OSError:
                        pass
            return
        else:
            from . import history

            history.note_failure("archive", "pw-record not found; audio not saved")
    F.atomic_write_text(rt / "rec-start.json", json.dumps(info))


def _claim(record_id: object) -> bool:
    """True once per record id (atomic O_EXCL marker in the runtime dir)."""
    from . import rtdir

    try:
        rt = rtdir.runtime_path()
    except OSError:
        return False
    if not rtdir.claim(rt, record_id):
        return False
    rtdir.prune(rt, time.time(), rtdir.CLAIM_KEEP_S, "claimed")
    return True


def _discard_unconsumed_recording(entered_at: float) -> None:
    """A rec-start.json still present at post-output was never claimed by a
    transcription (cancel / no speech): stop its recorder, delete its WAV.
    Only a recording that started before this hook ran is touched; a newer
    one (the user already started the next dictation) is left alone."""
    from . import capture

    p = capture.rec_start_path()
    try:
        info = json.loads(p.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(info, dict):
        return
    start = info.get("start")
    state, _ = capture.read_state()
    if not isinstance(start, (int, float)) or start >= entered_at or state == "recording":
        return
    # Claim atomically, then make sure we claimed the generation we checked.
    claimed = p.with_name(f"rec-discard-{os.getpid()}.json")
    try:
        os.rename(p, claimed)
        mine = json.loads(claimed.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(mine, dict) or mine.get("start") != start:
        try:
            os.link(claimed, p)  # put a newer recording's hand-off back (never clobbers)
        except OSError:
            pass
        try:
            claimed.unlink()
        except OSError:
            pass
        return
    try:
        claimed.unlink()
    except OSError:
        pass
    pid = int(info.get("recorder_pid") or 0)
    for _ in range(60):  # the supervisor stops it once voxtype leaves `recording`
        if not pid:
            break
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.05)
    wav = info.get("wav")
    if isinstance(wav, str) and wav.startswith(str(F.runtime_dir())):
        try:
            os.unlink(wav)
        except OSError:
            pass


def on_post_output(entered_at: float | None = None, record_id: str | None = None) -> None:
    """Runs in the detached process (see hook_main): voxtype waits for this
    hook before it goes idle, so nothing here may run in the hook itself.
    ``entered_at`` = when voxtype started the hook (taken before the fork),
    so a recording that began after this hook is never touched.
    ``record_id`` = the record the hook claimed before the fork ("" = none:
    a cancelled / no-speech recording); ``None`` = not bound (run by hand),
    then the newest unclaimed record is used."""
    from . import history

    entered_at = time.time() if entered_at is None else entered_at
    # The window the text was just delivered to, read before any slower work
    # below, so the learning watcher binds to it and not to whatever has focus
    # once maintenance and the archive are done.
    delivery_window = _hypr_active_window()
    feats = F.load()
    if record_id is None:
        rec = history.latest()
        if rec is not None and not _claim(rec.get("id")):
            # Voxtype also runs post_output after a cancelled / no-speech
            # recording (measured): the newest record is then an older
            # dictation that was already handled. Never archive or watch it
            # twice; drop the cancelled recording's scratch audio instead.
            rec = None
    else:
        rec = history.handoff(record_id) if record_id else None
    if rec is None:
        _discard_unconsumed_recording(entered_at)
    try:
        try:
            history.maintain(feats=feats)
        except Exception as e:
            history.note_failure("post-output", f"maintain: {e}")
        if rec and feats["archive"]["enabled"]:
            try:
                from . import archive

                archive.finalize(rec, feats)
            except Exception as e:
                history.note_failure("archive", str(e))
        if rec and rec.get("learned_hits"):
            try:
                from . import learned  # type: ignore[attr-defined]

                learned.record_hits(rec["learned_hits"])
            except Exception:
                pass
        if rec and feats["learning"]["enabled"]:
            try:
                from . import learn_watch  # type: ignore[attr-defined]

                try:
                    learn_watch.on_post_output(rec, window=delivery_window)
                except TypeError:  # an older learn_watch without the parameter
                    learn_watch.on_post_output(rec)
            except ImportError:
                pass
            except Exception as e:
                history.note_failure("learn_watch", str(e))
    except Exception as e:
        history.note_failure("post-output", repr(e))
    finally:
        if rec:
            history.remove_handoff(str(rec.get("id")))  # handled: no text left in RAM


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    try:
        if event == "pre-recording":
            on_pre_recording()
        elif event == "post-output":
            started = os.environ.get("VOXTYPE_TUI_HOOK_STARTED")
            on_post_output(float(started) if started else None,
                           record_id=os.environ.get("VOXTYPE_TUI_HOOK_RECORD"))
        elif event == "pre-output":
            pass
        elif event == "--unwire":
            print(json.dumps(unwire_all()))
        elif event == "--sync":
            print(json.dumps(sync_config()))
        elif event == "--maintain":
            from . import history

            history.maintain()
        else:
            sys.stderr.write("usage: voxtype-tui-hook pre-recording|pre-output|post-output\n")
    except Exception as e:  # never break dictation
        try:
            from . import history

            history.note_failure(f"hook {event}", repr(e))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
