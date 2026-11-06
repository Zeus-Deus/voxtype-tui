"""'Learn everywhere': let more apps expose their text to accessibility.

Learning from in-app corrections reads the corrected text over AT-SPI (see
``learn_watch``). GTK apps expose it out of the box. Qt apps, Chromium-based
browsers and Electron apps only do when asked, so this module manages a few
small, clearly marked edits:

* ``environment.d/90-voxtype-tui-accessibility.conf`` (whole file ours):
  ``NO_AT_BRIDGE=0``, ``QT_ACCESSIBILITY=1``,
  ``QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1``, ``ACCESSIBILITY_ENABLED=1``. Read
  by the systemd user manager at login; Omarchy's uwsm session hands it to
  every app.
* ``--force-renderer-accessibility`` appended, under a marker comment, to
  the flags file of every installed Chromium-family browser and system
  Electron (``chromium-flags.conf``, ``brave-flags.conf``,
  ``chrome-flags.conf``, ``electronNN-flags.conf`` / ``electron-flags.conf``,
  ``obsidian/user-flags.conf``). Only for programs that are installed.

Rules: nothing is written without an explicit :func:`set_enabled` call;
:func:`preview` shows the exact files and diffs first. Disabling removes only
the lines we added (later user edits in those files are kept) and deletes a
file only if we created it and nothing else is left in it. Terminal key
capture is not part of this release. Stdlib only.

The change needs a re-login for apps started before it. As a convenience,
enabling also sets the variables in the running user manager
(``systemctl --user set-environment``) and turns on the accessibility bus's
``org.a11y.Status.IsEnabled`` for this session, so browsers restarted
afterwards already work; disabling unsets the variables and puts
``IsEnabled`` back if we changed it (both skipped when
``VOXTYPE_TUI_SYSTEMCTL=0``).
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

MARKER = "# Managed by voxtype-tui (Learn everywhere): lets Voxtype learn from your corrections"
FLAG = "--force-renderer-accessibility"
ENV_FILE_NAME = "90-voxtype-tui-accessibility.conf"
ENV_VARS = (
    ("NO_AT_BRIDGE", "0"),
    ("QT_ACCESSIBILITY", "1"),
    ("QT_LINUX_ACCESSIBILITY_ALWAYS_ON", "1"),
    # Chromium/Electron only build their accessibility bridge when the
    # platform says a11y is on; the flag alone is not enough (VM-verified).
    ("ACCESSIBILITY_ENABLED", "1"),
)
ENV_HEADER = (
    "# Managed by voxtype-tui: lets apps expose their text to the accessibility bus\n"
    "# so Voxtype can learn from corrections you make after a dictation.\n"
    "# Turn off \"Learn everywhere\" in Voxtype to remove this file.\n"
)


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def _state_path() -> Path:
    return _config_home() / "voxtype-tui" / "learn-everywhere.json"


def env_file() -> Path:
    return _config_home() / "environment.d" / ENV_FILE_NAME


def _env_content() -> str:
    return ENV_HEADER + "".join(f"{k}={v}\n" for k, v in ENV_VARS)


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _system_electrons() -> list[str]:
    names = set()
    for d in os.environ.get("PATH", "/usr/bin").split(os.pathsep):
        with contextlib.suppress(OSError):
            for n in os.listdir(d):
                if n.startswith("electron") and (n == "electron" or n[8:].isdigit()):
                    names.add(n)
    return sorted(names)


def flag_files() -> list[tuple[Path, str]]:
    """``(flags file, why)`` for every installed program that reads one."""
    c = _config_home()
    out: list[tuple[Path, str]] = []
    if _which("chromium"):
        out.append((c / "chromium-flags.conf", "Chromium"))
    if _which("brave") or _which("brave-browser"):
        out.append((c / "brave-flags.conf", "Brave"))
    if _which("google-chrome-stable") or _which("google-chrome"):
        out.append((c / "chrome-flags.conf", "Google Chrome"))
    fallback = c / "electron-flags.conf"
    for name in _system_electrons():
        own = c / f"{name}-flags.conf"
        # The Arch wrapper reads electronNN-flags.conf, else electron-flags.conf:
        # creating the first would hide a user's fallback file, so edit that.
        target = own if (own.exists() or not fallback.exists() or name == "electron") else fallback
        if all(p != target for p, _ in out):
            out.append((target, f"Electron apps ({name})"))
    if _which("obsidian"):
        out.append((c / "obsidian" / "user-flags.conf", "Obsidian"))
    return out


# ------------------------------------------------------------------ state

def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "enabled": False, "files": {}}


def _atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with contextlib.suppress(OSError):
            st = path.stat()
            mode = st.st_mode & 0o7777
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _save_state(st: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p.parent, 0o700)
    _atomic_write(p, json.dumps(st, indent=2) + "\n", 0o600)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


# ------------------------------------------------------------------ edits

def _flag_present(text: str) -> bool:
    return any(line.strip() == FLAG for line in text.splitlines())


def _enable_flags_text(old: str | None) -> tuple[str | None, dict]:
    """New content for a flags file, plus the bookkeeping to undo it."""
    if old is not None and _flag_present(old):
        return None, {"reason": "already set by you"}
    base = old or ""
    added_nl = bool(base) and not base.endswith("\n")
    new = base + ("\n" if added_nl else "") + MARKER + "\n" + FLAG + "\n"
    return new, {"kind": "flags", "created": old is None, "added_newline": added_nl}


def _disable_flags_text(cur: str, rec: dict) -> str | None:
    """Remove our marker + the flag line right after it. None = delete file."""
    lines = cur.splitlines(keepends=True)
    out = []
    block_at_end = False
    i = 0
    while i < len(lines):
        if lines[i].rstrip("\n") == MARKER:
            i += 1
            if i < len(lines) and lines[i].strip() == FLAG:
                i += 1
            block_at_end = i >= len(lines)
            continue
        out.append(lines[i])
        i += 1
    new = "".join(out)
    if rec.get("added_newline") and block_at_end and new.endswith("\n"):
        new = new[:-1]  # we added that newline before our block
    if rec.get("created") and new.strip() == "":
        return None
    return new


def _disable_env_text(cur: str) -> str | None | bool:
    """Remove our lines; None = delete the file; False = not ours any more
    (our header is gone: the user replaced the file), leave it alone."""
    header = set(ENV_HEADER.splitlines())
    if not header & set(cur.splitlines()):
        return False
    ours = {f"{k}={v}" for k, v in ENV_VARS}
    kept = [ln for ln in cur.splitlines(keepends=True)
            if ln.rstrip("\n") not in ours and ln.rstrip("\n") not in header]
    if "".join(kept).strip() == "":
        return None
    return "".join(kept)


def _plan(enable: bool) -> list[dict]:
    """Every file and the exact change :func:`set_enabled` would make."""
    st = _load_state()
    plan = []
    envp = env_file()
    cur = _read(envp)
    if enable:
        want = _env_content()
        if envp.is_symlink():
            plan.append({"path": envp, "action": "none", "old": cur, "new": cur, "rec": None,
                         "reason": "a symlink, not ours"})
        elif cur == want:
            plan.append({"path": envp, "action": "none", "old": cur, "new": cur,
                         "rec": st["files"].get(str(envp)) or {"kind": "env", "created": False},
                         "reason": "already in place"})
        elif cur is None:
            plan.append({"path": envp, "action": "create", "old": None, "new": want,
                         "rec": {"kind": "env", "created": True}})
        else:
            plan.append({"path": envp, "action": "none", "old": cur, "new": cur, "rec": None,
                         "reason": "a file with this name exists and is not ours"})
        for path, why in flag_files():
            if path.is_symlink():
                plan.append({"path": path, "action": "none", "old": None, "new": None, "rec": None,
                             "reason": f"a symlink; add {FLAG} to it yourself", "app": why})
                continue
            old = _read(path)
            new, rec = _enable_flags_text(old)
            if new is None:
                plan.append({"path": path, "action": "none", "old": old, "new": old,
                             "rec": None, "reason": rec.get("reason"), "app": why})
            else:
                plan.append({"path": path, "action": "create" if old is None else "append",
                             "old": old, "new": new, "rec": rec, "app": why})
    else:
        for p, rec in st["files"].items():
            path = Path(p)
            if path.is_symlink():
                plan.append({"path": path, "action": "none", "old": None, "new": None, "rec": rec,
                             "reason": f"now a symlink; remove the {FLAG} line yourself"})
                continue
            curp = _read(path)
            if curp is None:
                plan.append({"path": path, "action": "none", "old": None, "new": None,
                             "rec": rec, "reason": "already gone"})
                continue
            new = _disable_env_text(curp) if rec.get("kind") == "env" else _disable_flags_text(curp, rec)
            if new is False or new == curp:
                plan.append({"path": path, "action": "none", "old": curp, "new": curp, "rec": rec,
                             "reason": "no longer contains our lines"})
                continue
            plan.append({"path": path, "action": "remove" if new is None else "edit",
                         "old": curp, "new": new, "rec": rec})
    return plan


def _diff(item: dict) -> str:
    old = item["old"] or ""
    new = item["new"] or ""
    if old == new:
        return ""
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=str(item["path"]) if item["old"] is not None else "/dev/null",
        tofile=str(item["path"]) if item["new"] is not None else "/dev/null",
    ))


def is_enabled() -> bool:
    return bool(_load_state().get("enabled"))


def preview(enable: bool | None = None) -> dict:
    """``{"enabled", "files":[{path, action, diff, reason?, app?}], "needs_relogin"}``.

    ``enable`` defaults to the opposite of the current state (what the
    toggle would do)."""
    if enable is None:
        enable = not is_enabled()
    plan = _plan(enable)
    files = []
    for it in plan:
        row = {"path": str(it["path"]), "action": it["action"], "diff": _diff(it)}
        if it.get("reason"):
            row["reason"] = it["reason"]
        if it.get("app"):
            row["app"] = it["app"]
        files.append(row)
    return {"enabled": is_enabled(), "enable": enable, "files": files,
            "needs_relogin": any(f["action"] != "none" for f in files)}


def _bus_enabled() -> bool | None:
    try:
        p = subprocess.run(["busctl", "--user", "get-property", "org.a11y.Bus", "/org/a11y/bus",
                            "org.a11y.Status", "IsEnabled"], capture_output=True, text=True,
                           timeout=3, check=False)
        v = p.stdout.strip()
        return True if v == "b true" else False if v == "b false" else None
    except Exception:
        return None


def _set_bus_enabled(value: bool) -> None:
    with contextlib.suppress(Exception):
        subprocess.run(["busctl", "--user", "set-property", "org.a11y.Bus", "/org/a11y/bus",
                        "org.a11y.Status", "IsEnabled", "b", "true" if value else "false"],
                       timeout=3, check=False, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _manager_env() -> dict[str, str] | None:
    try:
        p = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                           text=True, timeout=5, check=False)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    env = {}
    for line in p.stdout.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            env[k] = v
    return env


def _systemctl(*args: str) -> None:
    with contextlib.suppress(Exception):
        subprocess.run(["systemctl", "--user", *args], timeout=5, check=False,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


def _session_env(enable: bool, st: dict) -> None:
    """Live-session convenience (not persisted). Records the previous values
    in ``st`` and, on disable, restores only what is still our value."""
    if os.environ.get("VOXTYPE_TUI_SYSTEMCTL") == "0" or not _which("systemctl"):
        return
    ours = dict(ENV_VARS)
    if enable:
        cur = _manager_env()
        if cur is not None:
            prev = st.get("session_env_prev")
            if not isinstance(prev, dict):  # keep the first, pre-enable values
                st["session_env_prev"] = {k: cur.get(k) for k in ours}
            _systemctl("set-environment", *[f"{k}={v}" for k, v in ENV_VARS])
    else:
        prev = st.pop("session_env_prev", None)
        cur = _manager_env() if isinstance(prev, dict) else None
        if isinstance(prev, dict) and cur is not None:
            unset = [k for k, v in ours.items() if cur.get(k) == v and prev.get(k) is None]
            reset = [f"{k}={prev[k]}" for k, v in ours.items()
                     if cur.get(k) == v and isinstance(prev.get(k), str)]
            if unset:
                _systemctl("unset-environment", *unset)
            if reset:
                _systemctl("set-environment", *reset)
    if not _which("busctl"):
        return
    if enable:
        if _bus_enabled() is False:
            _set_bus_enabled(True)
            st["bus_was_disabled"] = True
    elif st.pop("bus_was_disabled", False):
        _set_bus_enabled(False)


def _set_feature(enabled: bool) -> None:
    with contextlib.suppress(Exception):
        import importlib
        features = importlib.import_module("voxtype_tui.features")
        try:  # no hook rewiring: "everywhere" needs no Voxtype setting
            features.update({"learning": {"everywhere": enabled}}, wire=False)
        except TypeError:
            features.update({"learning": {"everywhere": enabled}})


@contextlib.contextmanager
def _locked():
    import fcntl

    d = _state_path().parent
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / "learn-everywhere.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def set_enabled(enabled: bool) -> dict:
    """Apply (or undo) the managed edits. Returns
    ``{"ok", "enabled", "files":[{path, action}], "needs_relogin"}``."""
    with _locked():
        return _set_enabled_locked(bool(enabled))


def _set_enabled_locked(enabled: bool) -> dict:
    plan = _plan(enabled)
    st = _load_state()
    done = []
    try:
        for it in plan:
            path: Path = it["path"]
            if enabled and it["rec"] is not None and it["action"] != "none":
                # record ownership BEFORE touching the file, so a crash
                # between the two can still be undone
                st["files"][str(path)] = it["rec"]
                _save_state(st)
            if it["action"] in ("create", "append", "edit"):
                _atomic_write(path, it["new"])
            elif it["action"] == "remove":
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            if not enabled and it["action"] in ("edit", "remove", "none"):
                st["files"].pop(str(path), None)
            done.append({"path": str(path), "action": it["action"]})
    except OSError as e:
        _save_state(st)
        return {"ok": False, "error": f"could not write {e.filename or ''}: {e.strerror or e}",
                "files": done}
    st["enabled"] = enabled
    _session_env(enabled, st)
    _save_state(st)
    _set_feature(enabled)
    return {"ok": True, "enabled": enabled, "files": done,
            "needs_relogin": any(d["action"] != "none" for d in done)}
