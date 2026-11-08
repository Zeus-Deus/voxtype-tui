"""The per-dictation runtime dir and record handoffs.

Imports only ``os``/``stat``: the hook entry point uses it before it forks,
while voxtype waits.

Handoffs: the post-process CLI writes one file per dictation,
``handoff/<record id>.json``; it is never overwritten by a later dictation.
The post-output hook binds itself to a record *before* it forks: voxtype runs
post_process → output → post_output and waits for post_output before the
next recording, so at that moment the newest handoff is this dictation's (or
an older, already-claimed one after a cancelled recording). The record id is
claimed once (an O_EXCL marker in ``claimed/``) and handed to the detached
worker, so a worker that starts late never picks up a newer dictation.
"""
import os
import stat

HANDOFF_MAX_AGE_S = 120.0
CLAIM_KEEP_S = 3600.0


class UnsafeRuntimeDir(OSError):
    """The runtime dir exists but someone else could control its contents."""


def runtime_path() -> str:
    """``$XDG_RUNTIME_DIR/voxtype-tui``, else ``<tmp>/voxtype-tui-<uid>``.
    Created 0700 when missing. In a shared /tmp another user may have
    created it first, so it is refused (:class:`UnsafeRuntimeDir`) unless
    it is a real directory (not a symlink) owned by us and not writable by
    group or others."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        import tempfile

        path = os.path.join(tempfile.gettempdir(), f"voxtype-tui-{os.getuid()}")
    else:
        path = os.path.join(base, "voxtype-tui")
        if not os.path.isdir(base):
            os.makedirs(base, mode=0o700, exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
            or st.st_mode & 0o022):
        raise UnsafeRuntimeDir(f"refusing unsafe runtime dir {path}")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)  # ours and never writable by others: just tighten
    return path


def _subdir(rt: str, name: str) -> str:
    d = os.path.join(rt, name)
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    return d


def valid_id(record_id: object) -> bool:
    return (isinstance(record_id, str) and 0 < len(record_id) <= 64
            and all(c.isalnum() or c in "-_" for c in record_id))


def handoff_file(rt: str, record_id: str) -> str:
    if not valid_id(record_id):
        raise ValueError("bad record id")
    return os.path.join(rt, "handoff", record_id + ".json")


def claim(rt: str, record_id: object) -> bool:
    """True once per record id (atomic O_EXCL marker)."""
    if not valid_id(record_id):
        return False
    d = _subdir(rt, "claimed")
    try:
        fd = os.open(os.path.join(d, str(record_id)), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except OSError:
        return False
    return True


def newest_handoff(rt: str, max_age_s: float = HANDOFF_MAX_AGE_S) -> "str | None":
    """Id of the most recently written handoff, when written in the last
    ``max_age_s``, or None. By write time: ids only sort to the second."""
    import time

    d = os.path.join(rt, "handoff")
    best = None
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if not e.name.endswith(".json") or not e.is_file(follow_symlinks=False):
                        continue
                    key = (e.stat(follow_symlinks=False).st_mtime_ns, e.name)
                except OSError:
                    # Removed while we scan (an older record's worker just
                    # finished): skip it, the others still count.
                    continue
                if best is None or key > best[0]:
                    best = (key, e.name)
    except OSError:
        return None
    if best is None or time.time() - best[0][0] / 1e9 > max_age_s:
        return None
    return best[1][:-5]


def claim_newest_handoff() -> str:
    """The post-output hook's record, claimed: an id, or "" when the newest
    handoff is already claimed (a cancelled recording's post-output) or
    there is none."""
    rt = runtime_path()
    rid = newest_handoff(rt)
    return rid if rid and claim(rt, rid) else ""


def prune(rt: str, now: float, keep_s: float, sub: str) -> None:
    """Remove regular files in ``rt/sub`` older than ``keep_s`` (never
    follows symlinks)."""
    d = os.path.join(rt, sub)
    try:
        if not stat.S_ISDIR(os.lstat(d).st_mode):
            return
        with os.scandir(d) as it:
            for e in it:
                try:
                    if (e.is_file(follow_symlinks=False)
                            and now - e.stat(follow_symlinks=False).st_mtime > keep_s):
                        os.unlink(e.path)
                except OSError:
                    continue  # gone already (or not ours to remove): keep going
    except OSError:
        pass
