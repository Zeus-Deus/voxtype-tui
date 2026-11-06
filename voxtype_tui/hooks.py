"""Voxtype hook wiring + the ``voxtype-tui-hook <event>`` entry point.

Wiring (DESIGN A3–A5)
---------------------
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
* ``post_output_command`` is wired when the archive is on or the learning
  watcher exists and learning is on. Voxtype waits for this hook before it
  goes idle, so the hook forks its work into a detached process and returns.
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
        POST_OUTPUT: bool(archive or learning),
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
                OUR_COMMANDS[key], (feats.get(F.MANAGED_HOOKS_KEY) or {}).get(key),
            ):
                foreign[key] = str(cur)
    reasons: dict[str, str] = {}
    pp = config.get_post_process(doc).get("command")
    pp_foreign = bool(pp) and str(pp) != config.POSTPROCESS_COMMAND
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
        recorded = plan.managed.get(key)
        cur = _get(doc, key)
        cur_s = None if cur is None else str(cur)
        is_ours = cur_s is not None and (cur_s == ours or (recorded is not None and cur_s == recorded))
        if want[key]:
            if cur_s is None or cur_s == "" or is_ours:
                if cur_s != ours:
                    _set_output(doc, key, ours)
                    plan.changed = True
                    plan.wired.append(key)
                plan.managed[key] = ours
            else:
                plan.foreign[key] = cur_s
                plan.managed.pop(key, None)
        else:
            if is_ours:
                _del_output(doc, key)
                plan.changed = True
                plan.unwired.append(key)
            elif cur_s is not None and cur_s != "":
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
    plan = reconcile(doc, feats)
    if plan.changed:
        config.safe_save(doc, path)
    if plan.managed != (feats.get(F.MANAGED_HOOKS_KEY) or {}):
        F.set_managed_hooks(plan.managed, features_file)
    return {"changed": plan.changed, "wired": plan.wired, "unwired": plan.unwired,
            "unavailable": plan.unavailable}


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
            if cur is not None and str(cur) in (OUR_COMMANDS[key], managed.get(key)):
                _del_output(doc, key)
                changed = True
        if changed:
            config.safe_save(doc, path, validate=False)
    if managed:
        F.set_managed_hooks({}, features_file)
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


def _purge_stale_wavs(rec_dir: Path, keep: str | None = None) -> None:
    try:
        for p in rec_dir.glob("*.wav"):
            if str(p) != keep and time.time() - p.stat().st_mtime > 600:
                p.unlink()
    except OSError:
        pass


def _supervise(rec_pid: int, cap_s: float) -> None:
    """Detached: stop the recorder once voxtype leaves `recording`."""
    import signal

    from . import capture

    deadline = time.time() + cap_s
    time.sleep(0.2)
    while time.time() < deadline:
        state, _ = capture.read_state()
        if state != "recording":
            break
        try:
            os.kill(rec_pid, 0)
        except OSError:
            return
        time.sleep(0.05)
    try:
        os.kill(rec_pid, signal.SIGINT)
    except OSError:
        return
    for _ in range(100):
        try:
            os.kill(rec_pid, 0)
        except OSError:
            return
        time.sleep(0.05)
    try:
        os.kill(rec_pid, signal.SIGKILL)
    except OSError:
        pass


def on_pre_recording() -> None:
    import subprocess

    from . import capture

    feats = F.load()
    state, mtime = capture.read_state()
    now = time.time()
    start = mtime if state == "recording" and mtime and now - mtime < 5 else now
    info: dict[str, Any] = {"start": start, "hook_ts": now, "app": None}
    archive = feats["archive"]
    if not (feats["usage"]["enabled"] or feats["history"]["enabled"] or archive["enabled"]):
        return
    info["app"] = _hypr_active_window()
    rt = F.ensure_private_dir(F.runtime_dir())
    if archive["enabled"] and archive["save_audio"]:
        binary = recorder_binary()
        if binary:
            cfg = capture.config_info()
            rec_dir = F.ensure_private_dir(rt / "rec")
            wav = str(rec_dir / f"{int(start * 1000)}.wav")
            _purge_stale_wavs(rec_dir, keep=wav)
            old_umask = os.umask(0o077)
            try:
                proc = subprocess.Popen(
                    _recorder_argv(binary, cfg.get("device", "default"), wav),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                )
            finally:
                os.umask(old_umask)
            info.update({"wav": wav, "recorder_pid": proc.pid, "recorder_started": time.time(),
                         "device": cfg.get("device", "default"),
                         "recorder": os.path.basename(binary)})
            F.atomic_write_text(rt / "rec-start.json", json.dumps(info))
            # Already detached (hook_main): supervise the recorder right here.
            _supervise(proc.pid, float(cfg.get("max_duration_secs") or 60) + 5)
            return
        else:
            from . import history

            history.note_failure("archive", "pw-record not found; audio not saved")
    F.atomic_write_text(rt / "rec-start.json", json.dumps(info))


def on_post_output() -> None:
    """Runs in the detached process (see hook_main): voxtype waits for this
    hook before it goes idle, so nothing here may run in the hook itself."""
    from . import history

    feats = F.load()
    rec = history.latest()
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

                learn_watch.on_post_output(rec)
            except ImportError:
                pass
            except Exception as e:
                history.note_failure("learn_watch", str(e))
    except Exception as e:
        history.note_failure("post-output", repr(e))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    try:
        if event == "pre-recording":
            on_pre_recording()
        elif event == "post-output":
            on_post_output()
        elif event == "pre-output":
            pass
        elif event == "--unwire":
            print(json.dumps(unwire_all()))
        elif event == "--sync":
            print(json.dumps(sync_config()))
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
