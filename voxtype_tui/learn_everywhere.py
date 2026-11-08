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
import errno
import json
import os
import shutil
import stat
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


def _atomic_write(path: Path, text: str, mode: int = 0o644, *,
                  check=None, exclusive: bool = False) -> None:
    """Write ``text`` to a temp file, then publish it in one step.

    ``check()`` runs right before publishing and may raise to abort (the
    target changed meanwhile). ``exclusive`` publishes with link(), which
    fails if the target exists, instead of replacing it."""
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
        if check is not None:
            check()
        if exclusive:
            os.link(tmp, path)
            os.unlink(tmp)
        else:
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


def _ident(path: Path) -> list[int] | None:
    """What the directory entry is right now (no symlink following):
    type, inode, size and mtime. None = absent."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return [st.st_dev, st.st_ino, st.st_mode & 0o170000, st.st_size, st.st_mtime_ns]


class _Conflict(Exception):
    """A target changed between planning and writing it. ``kept`` names
    where the claimed file was preserved if it could not go back."""

    def __init__(self, path: str, kept: str | None = None):
        super().__init__(path)
        self.kept = kept


def _expect(path: Path, item: dict) -> None:
    """Raise :class:`_Conflict` unless ``path`` is still exactly what the
    plan saw (same entry, not a symlink, same bytes)."""
    if _ident(path) != item.get("ident"):
        raise _Conflict(str(path))
    if item.get("ident") is not None and (path.is_symlink() or _read(path) != item["old"]):
        raise _Conflict(str(path))


def _race_hook(stage: str, path: Path) -> None:
    """Does nothing. Tests replace it to save a file at an exact moment
    ("append": right before our bytes are written; "claim": right before
    the target is moved aside; "publish": right before the edited copy is
    linked into place)."""


def _same_file(st: os.stat_result, ident: list[int] | None) -> bool:
    return (ident is not None and stat.S_ISREG(st.st_mode)
            and [st.st_dev, st.st_ino] == list(ident[:2]))


def _read_fd(fd: int) -> bytes:
    chunks, off = [], 0
    while True:
        b = os.pread(fd, 65536, off)
        if not b:
            return b"".join(chunks)
        chunks.append(b)
        off += len(b)


def _read_nofollow(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return _read_fd(fd)
    finally:
        os.close(fd)


def _append_if_unchanged(path: Path, item: dict) -> None:
    """Add our block to an existing file in place, never by replacing it.

    The file is opened without following symlinks and in append mode, then
    checked through that same descriptor: it must be the very file the plan
    read (same device and inode) with the same bytes. A save landing after
    that check is never lost: an in-place write keeps both, a save by
    rename leaves the new file alone (our bytes went to the old copy) and is
    reported as a conflict."""
    old, new = item["old"], item["new"]
    if not isinstance(old, str) or not new.startswith(old):
        raise _Conflict(str(path))
    _expect(path, item)
    try:
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOENT, errno.ENXIO, errno.EISDIR):
            raise _Conflict(str(path)) from None
        raise
    try:
        st = os.fstat(fd)
        if not _same_file(st, item.get("ident")) or _read_fd(fd) != old.encode("utf-8"):
            raise _Conflict(str(path))
        _race_hook("append", path)
        data = new[len(old):].encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
        try:
            now = os.lstat(path)
        except FileNotFoundError:
            now = None
        if now is None or (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
            raise _Conflict(str(path))  # renamed over meanwhile: the live file is theirs
    finally:
        os.close(fd)


def _put_back(tmpdir: str, aside: Path, path: Path) -> str | None:
    """Return a claimed entry to ``path`` untouched (a symlink stays a
    symlink). If ``path`` was recreated meanwhile, the newer file stays and
    the claimed one is kept next to it as ``<name>.voxtype-tui-kept``;
    returns that path."""
    try:
        try:
            os.link(aside, path, follow_symlinks=False)
            os.unlink(aside)
            return None
        except FileExistsError:
            pass
        except PermissionError:  # a directory cannot be hard-linked
            if not os.path.lexists(path):
                os.rename(aside, path)
                return None
        for n in range(100):
            kept = path.with_name(f"{path.name}.voxtype-tui-kept" + (f".{n}" if n else ""))
            try:
                os.link(aside, kept, follow_symlinks=False)
                os.unlink(aside)
                return str(kept)
            except FileExistsError:
                continue
        return str(aside)
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(tmpdir)


def _claim(path: Path, item: dict) -> tuple[str, Path, os.stat_result]:
    """Move ``path`` into a private directory beside it (one atomic rename,
    which moves a symlink itself, never its target) and check that what
    moved is the file the plan read: same inode, same bytes. Otherwise it
    is put back untouched and :class:`_Conflict` raised."""
    _expect(path, item)
    _race_hook("claim", path)
    tmpdir = tempfile.mkdtemp(prefix=f".{path.name}.voxtype-tui.", dir=str(path.parent))
    aside = Path(tmpdir) / path.name
    try:
        os.rename(path, aside)
    except BaseException as e:
        with contextlib.suppress(OSError):
            os.rmdir(tmpdir)
        if isinstance(e, FileNotFoundError):
            raise _Conflict(str(path)) from None
        raise
    try:
        st = os.lstat(aside)
        ok = _same_file(st, item.get("ident")) and _read_nofollow(aside) == item["old"].encode("utf-8")
    except BaseException:
        _put_back(tmpdir, aside, path)
        raise
    if not ok:
        raise _Conflict(str(path), _put_back(tmpdir, aside, path))
    return tmpdir, aside, st


def _replace_if_unchanged(path: Path, item: dict) -> None:
    """Swap in the cleaned content: claim the target, write the new copy,
    publish it with link() (fails if the name was recreated meanwhile, in
    which case the newer file wins and ours is kept beside it)."""
    tmpdir, aside, st = _claim(path, item)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".new.", dir=tmpdir)
        os.fchmod(fd, st.st_mode & 0o7777)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(item["new"])
            f.flush()
            os.fsync(f.fileno())
        _race_hook("publish", path)
        os.link(tmp, path)
    except BaseException as e:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        kept = _put_back(tmpdir, aside, path)
        if isinstance(e, FileExistsError):
            raise _Conflict(str(path), kept) from None
        raise
    os.unlink(tmp)
    os.unlink(aside)
    with contextlib.suppress(OSError):
        os.rmdir(tmpdir)


def _remove_if_unchanged(path: Path, item: dict) -> None:
    """Delete ``path`` only if it still holds what the plan saw."""
    tmpdir, aside, _st = _claim(path, item)
    os.unlink(aside)
    with contextlib.suppress(OSError):
        os.rmdir(tmpdir)


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
    env_ident = _ident(envp)
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
                         "rec": {"kind": "env", "created": True}, "ident": env_ident})
        else:
            plan.append({"path": envp, "action": "none", "old": cur, "new": cur, "rec": None,
                         "reason": "a file with this name exists and is not ours"})
        for path, why in flag_files():
            if path.is_symlink():
                plan.append({"path": path, "action": "none", "old": None, "new": None, "rec": None,
                             "reason": f"a symlink; add {FLAG} to it yourself", "app": why})
                continue
            ident = _ident(path)
            old = _read(path)
            new, rec = _enable_flags_text(old)
            if new is None:
                plan.append({"path": path, "action": "none", "old": old, "new": old,
                             "rec": None, "reason": rec.get("reason"), "app": why})
            else:
                plan.append({"path": path, "action": "create" if old is None else "append",
                             "old": old, "new": new, "rec": rec, "app": why, "ident": ident})
    else:
        for p, rec in st["files"].items():
            path = Path(p)
            if path.is_symlink():
                plan.append({"path": path, "action": "none", "old": None, "new": None, "rec": rec,
                             "reason": f"now a symlink; remove the {FLAG} line yourself"})
                continue
            ident = _ident(path)
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
                         "old": curp, "new": new, "rec": rec, "ident": ident})
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
            recorded = False
            if enabled and it["rec"] is not None and it["action"] != "none":
                # record ownership BEFORE touching the file, so a crash
                # between the two can still be undone
                recorded = str(path) not in st["files"]
                st["files"][str(path)] = it["rec"]
                _save_state(st)
            # Each target is checked again right before it is written: a
            # user or editor change since the plan is never overwritten.
            try:
                if it["action"] == "create":
                    _atomic_write(path, it["new"], check=lambda p=path, i=it: _expect(p, i),
                                  exclusive=True)
                elif it["action"] == "append":
                    _append_if_unchanged(path, it)
                elif it["action"] == "edit":
                    _replace_if_unchanged(path, it)
                elif it["action"] == "remove":
                    _remove_if_unchanged(path, it)
            except (_Conflict, FileExistsError) as e:
                if recorded:
                    st["files"].pop(str(path), None)
                _save_state(st)
                kept = getattr(e, "kept", None)
                out = {"ok": False, "conflict": str(path), "files": done,
                       "error": f"{path} changed while Learn everywhere was updating it; "
                                "nothing was written to it. Try again."}
                if kept:
                    out["kept"] = kept
                    out["error"] = (f"{path} was saved again while Learn everywhere was updating "
                                    f"it; your new file was kept and the previous one is at {kept}.")
                return out
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
