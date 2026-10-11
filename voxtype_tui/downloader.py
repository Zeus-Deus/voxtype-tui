"""Small verified HTTPS downloader for files voxtype itself can't fetch.

Used for the "extra" models (quantized whisper files, Qwen3-ASR GGUFs) and
the pinned CrispASR server build. Contract, in order:

  1. Stream the URL into ``<dest>.part`` (exclusive flock, so two
     downloads of the same file can't interleave).
  2. Hash while streaming; compare against the pinned sha256 (and the
     pinned size, when given) once the body is complete.
  3. fsync, then ``os.replace(<dest>.part, <dest>)`` — atomic, so
     ``<dest>`` is either absent, the previous file, or the complete
     verified new file. Never a truncated one.

Any failure (HTTP error, short read, checksum mismatch, cancel) removes the
owned safe ``.part`` and leaves ``<dest>`` untouched. Unsafe staging entries
or replaced inodes are refused and left alone. Cancellation is cooperative:
pass a ``threading.Event``; the read loop checks it between chunks, so a
SIGTERM handler that sets the event is enough to stop cleanly.

Stdlib only, no Textual import — the Omarchy plugin's bridge calls this
from its fast paths.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import stat
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

# Small enough that a cancel (checked between chunks) lands quickly even on
# a slow link; large enough that the per-chunk overhead is negligible.
CHUNK_BYTES = 256 * 1024
TIMEOUT_SECS = 30.0
USER_AGENT = "voxtype-tui"

# progress(done_bytes, total_bytes_or_None)
ProgressFn = Callable[[int, "int | None"], None]


class DownloadError(RuntimeError):
    """Download failed; message is safe to show to the user."""


class ChecksumError(DownloadError):
    """The body arrived complete but its sha256 (or size) is not the pinned one."""


class DownloadCancelled(DownloadError):
    """The cancel event was set before the download finished."""


def part_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".part")


def remove_stale_part(dest: Path) -> bool:
    """Remove ``dest.part`` unless a live download holds its lock (e.g.
    left behind by a SIGKILLed child). Returns True when removed."""
    part = part_path(Path(dest))
    try:
        fd = os.open(part, os.O_WRONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        part.unlink(missing_ok=True)
        return True
    finally:
        os.close(fd)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK_BYTES), b""):
            h.update(block)
    return h.hexdigest()


def download_verified(
    url: str,
    dest: Path,
    sha256: str,
    *,
    size: int | None = None,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    timeout: float = TIMEOUT_SECS,
) -> Path:
    """Download ``url`` to ``dest`` via ``dest.part``; verify; rename.

    Returns ``dest``. Raises :class:`DownloadCancelled`,
    :class:`ChecksumError` or :class:`DownloadError`; in every failure
    case ``dest`` is untouched. Only the owned safe ``.part`` is removed;
    unsafe pre-existing or replaced entries are never followed or unlinked.
    """
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        # Plain http only for the local test fixture.
        raise DownloadError(f"refusing non-HTTPS URL {url!r}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = part_path(dest)
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    except OSError as e:
        raise DownloadError(f"{dest.name}: unsafe staging entry ({e.strerror})") from e
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        # Another process owns this .part: leave it alone (no cleanup).
        raise DownloadError(f"{dest.name} is already being downloaded") from None
    try:
        if not _safe_staging(fd, part):
            raise DownloadError(f"{dest.name}: unsafe staging entry")
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("cancelled")
        os.ftruncate(fd, 0)
        _stream_into(fd, url, sha256.lower(), size, progress, cancel, timeout, dest.name)
        os.fsync(fd)
        if not _safe_staging(fd, part):
            raise DownloadError(f"{dest.name}: unsafe staging entry changed during download")
        os.replace(part, dest)
    except BaseException:
        # Never unlink another owner's replacement or an unsafe existing entry.
        if _safe_staging(fd, part):
            part.unlink(missing_ok=True)
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return dest


def _safe_staging(fd: int, part: Path) -> bool:
    """Require the locked, singly-linked regular inode still at this path."""
    try:
        opened, entry = os.fstat(fd), part.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
        and stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1
        and (opened.st_dev, opened.st_ino) == (entry.st_dev, entry.st_ino)
    )


def _stream_into(
    fd: int,
    url: str,
    want_sha: str,
    size: int | None,
    progress: ProgressFn | None,
    cancel: threading.Event | None,
    timeout: float,
    label: str,
) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("cancelled")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise DownloadError(f"{label}: HTTP {e.code} from {url}") from e
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise DownloadError(f"{label}: could not connect ({reason})") from e
    h = hashlib.sha256()
    done = 0
    with resp:
        header_len = resp.headers.get("Content-Length")
        total = size
        if total is None and header_len and header_len.isdigit():
            total = int(header_len)
        if progress is not None:
            progress(0, total)
        while True:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("cancelled")
            try:
                chunk = resp.read(CHUNK_BYTES)
            except (OSError, ValueError) as e:
                raise DownloadError(f"{label}: connection dropped ({e})") from e
            if not chunk:
                break
            h.update(chunk)
            view = memoryview(chunk)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            done += len(chunk)
            if size is not None and done > size:
                raise ChecksumError(f"{label}: server sent more than the pinned {size} bytes")
            if progress is not None:
                progress(done, total)
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("cancelled")
    if size is not None and done != size:
        raise DownloadError(f"{label}: incomplete download ({done} of {size} bytes)")
    got = h.hexdigest()
    if got != want_sha:
        raise ChecksumError(
            f"{label}: sha256 mismatch (expected {want_sha[:12]}…, got {got[:12]}…) — file discarded"
        )


def extract_tarball(tarball: Path, dest_dir: Path, *, expect_file: str) -> Path:
    """Extract a verified ``.tar.gz`` into a FRESH ``dest_dir``.

    The archive is extracted into a new empty temp dir next to
    ``dest_dir`` with tarfile's ``data`` filter (rejects absolute paths,
    ``..`` traversal, device files and links pointing outside). The
    archive must contain exactly one top-level directory holding
    ``expect_file``; that directory is then renamed to ``dest_dir``.
    An existing ``dest_dir`` is replaced only after the new one is
    complete.
    """
    dest_dir = Path(dest_dir)
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{dest_dir.name}.", dir=dest_dir.parent))
    try:
        try:
            with tarfile.open(tarball, "r:gz") as tf:
                tf.extractall(tmp, filter="data")
        except (tarfile.TarError, OSError) as e:
            raise DownloadError(f"could not extract {tarball.name}: {e}") from e
        entries = [p for p in tmp.iterdir()]
        if len(entries) != 1 or not entries[0].is_dir():
            raise DownloadError(
                f"{tarball.name}: expected one top-level directory, got "
                f"{sorted(p.name for p in entries)}"
            )
        root = entries[0]
        binary = root / expect_file
        if not binary.is_file() or binary.is_symlink():
            raise DownloadError(f"{tarball.name}: {expect_file} missing from archive")
        binary.chmod(binary.stat().st_mode | 0o100)
        if dest_dir.exists():
            old = dest_dir.with_name(dest_dir.name + ".old")
            shutil.rmtree(old, ignore_errors=True)
            os.rename(dest_dir, old)
            os.rename(root, dest_dir)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.rename(root, dest_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest_dir
