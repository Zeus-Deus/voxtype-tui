"""Cross-process single-instance guard.

Two voxtype-tui processes editing the same `~/.config/voxtype/config.toml`
+ `~/.config/voxtype-tui/sync.json` will lose updates silently — there's
no fcntl-style lock around the read-modify-write cycle. The realistic
trigger is one process from the AUR install + one from a conda dev env
running in parallel; the symptom (the user's "model changes by itself")
is whichever process saved last winning.

This module takes an exclusive `fcntl.flock` on a sibling lockfile at
process start and holds it for the lifetime of the TUI. A second
launcher gets `BlockingIOError` and we surface it cleanly with the
holder's PID before exiting non-zero. Crash-safe — flock releases when
the process dies, no manual cleanup needed.
"""
from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path

LOCK_PATH = Path.home() / ".config" / "voxtype-tui" / ".lock"


@dataclass
class LockResult:
    """Outcome of `acquire`. The fd is held for the process lifetime
    when `acquired=True` — let the OS reclaim it on exit."""
    acquired: bool
    holder_pid: int | None
    fd: int | None


def acquire(lock_path: Path | None = None) -> LockResult:
    """Try to take an exclusive lock on `lock_path`. Non-blocking.

    On success the fd is left open (intentionally leaked into the
    process so the kernel holds the lock until exit). On contention
    we read the existing PID from the file's contents and return it
    so the caller can show a useful error.
    """
    path = lock_path or LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_RDWR + O_CREAT so we can both write our PID and read an
    # existing one. Don't truncate — preserve the holder's PID for
    # the error message in the contention path.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder_pid = _read_pid(fd)
        os.close(fd)
        return LockResult(acquired=False, holder_pid=holder_pid, fd=None)
    # Got the lock. Truncate + write our PID so a contending process
    # has something useful to display.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    return LockResult(acquired=True, holder_pid=None, fd=fd)


def _read_pid(fd: int) -> int | None:
    """Best-effort read of the PID stored in the lockfile. Returns None
    when the file is empty or unparseable — the caller handles that
    gracefully ("another voxtype-tui instance is running")."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, 32).decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    if not data:
        return None
    try:
        return int(data.split()[0])
    except (ValueError, IndexError):
        return None
