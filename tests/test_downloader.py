"""Verified downloader: checksum mismatch, cancel, atomic rename, tar
extraction. Served by a local http.server — no real network."""
from __future__ import annotations

import hashlib
import http.server
import io
import os
import tarfile
import threading
import time
from pathlib import Path

import pytest

from voxtype_tui import downloader

BODY = os.urandom(3 * downloader.CHUNK_BYTES + 123)
SHA = hashlib.sha256(BODY).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, bytes] = {}
    slow: set[str] = set()

    def do_GET(self) -> None:  # noqa: N802
        body = self.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        step = 64 * 1024
        for i in range(0, len(body), step):
            try:
                self.wfile.write(body[i:i + step])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if self.path in self.slow:
                time.sleep(0.05)

    def log_message(self, *a) -> None:
        pass


@pytest.fixture
def server():
    _Handler.routes = {"/model.bin": BODY}
    _Handler.slow = set()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", _Handler
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_download_verifies_and_renames(server, tmp_path: Path) -> None:
    base, _ = server
    dest = tmp_path / "ggml-x.bin"
    seen: list[tuple[int, int | None]] = []
    out = downloader.download_verified(
        f"{base}/model.bin", dest, SHA, size=len(BODY),
        progress=lambda d, t: seen.append((d, t)),
    )
    assert out == dest
    assert dest.read_bytes() == BODY
    assert not downloader.part_path(dest).exists()
    assert seen[0] == (0, len(BODY)) and seen[-1] == (len(BODY), len(BODY))


def test_checksum_mismatch_discards_and_keeps_existing(server, tmp_path: Path) -> None:
    base, _ = server
    dest = tmp_path / "ggml-x.bin"
    dest.write_bytes(b"previous good file")
    with pytest.raises(downloader.ChecksumError, match="sha256 mismatch"):
        downloader.download_verified(f"{base}/model.bin", dest, "0" * 64, size=len(BODY))
    # Destination untouched, no .part left behind.
    assert dest.read_bytes() == b"previous good file"
    assert not downloader.part_path(dest).exists()


def test_size_mismatch_is_rejected(server, tmp_path: Path) -> None:
    base, _ = server
    dest = tmp_path / "f.bin"
    with pytest.raises(downloader.DownloadError, match="incomplete"):
        downloader.download_verified(f"{base}/model.bin", dest, SHA, size=len(BODY) + 10)
    with pytest.raises(downloader.ChecksumError, match="more than"):
        downloader.download_verified(f"{base}/model.bin", dest, SHA, size=len(BODY) - 10)
    assert not dest.exists() and not downloader.part_path(dest).exists()


def test_cancel_mid_download_removes_part(server, tmp_path: Path) -> None:
    base, handler = server
    handler.slow.add("/model.bin")
    dest = tmp_path / "f.bin"
    cancel = threading.Event()
    saw_part: list[bool] = []

    def progress(done: int, _total) -> None:
        if done >= downloader.CHUNK_BYTES:
            saw_part.append(downloader.part_path(dest).exists())
            cancel.set()

    with pytest.raises(downloader.DownloadCancelled):
        downloader.download_verified(
            f"{base}/model.bin", dest, SHA, size=len(BODY), progress=progress, cancel=cancel,
        )
    assert saw_part == [True]  # it really was writing to .part
    assert not dest.exists()
    assert not downloader.part_path(dest).exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
@pytest.mark.parametrize("pre_cancel", [False, True])
def test_unsafe_staging_preserves_destination(server, tmp_path, kind, pre_cancel):
    base, _ = server
    dest = tmp_path / "verified.bin"
    dest.write_bytes(b"existing verified destination")
    part = downloader.part_path(dest)
    keeper = None
    if kind == "symlink":
        part.symlink_to(dest)
    elif kind == "hardlink":
        os.link(dest, part)
    elif kind == "fifo":
        os.mkfifo(part)
        # Keep the old blocking O_WRONLY open from hanging this regression.
        keeper = os.open(part, os.O_RDWR | os.O_NONBLOCK)
    else:
        part.mkdir()
    cancel = threading.Event()
    if pre_cancel:
        cancel.set()
    error = None
    try:
        try:
            downloader.download_verified(f"{base}/model.bin", dest, SHA, cancel=cancel)
        except Exception as e:
            error = e
        assert dest.read_bytes() == b"existing verified destination"
        assert isinstance(error, downloader.DownloadError)
        assert not isinstance(error, downloader.DownloadCancelled)
        assert "unsafe staging" in str(error)
    finally:
        if keeper is not None:
            os.close(keeper)


def test_replaced_staging_entry_is_not_published_or_removed(server, tmp_path):
    base, _ = server
    dest = tmp_path / "verified.bin"
    dest.write_bytes(b"existing verified destination")
    part = downloader.part_path(dest)
    swapped = False
    def progress(done, total):
        nonlocal swapped
        if not swapped:
            part.unlink()
            part.symlink_to(dest)
            swapped = True
    error = None
    try:
        downloader.download_verified(f"{base}/model.bin", dest, SHA, progress=progress)
    except downloader.DownloadError as e:
        error = e
    assert not dest.is_symlink()
    assert dest.read_bytes() == b"existing verified destination"
    assert error is not None and "unsafe staging" in str(error)
    assert part.is_symlink()  # not the inode owned by this download


def test_pre_cancel_preserves_destination_and_removes_safe_staging(server, tmp_path):
    base, _ = server
    dest = tmp_path / "verified.bin"
    dest.write_bytes(b"existing verified destination")
    downloader.part_path(dest).write_bytes(b"old partial")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(downloader.DownloadCancelled):
        downloader.download_verified(f"{base}/model.bin", dest, SHA, cancel=cancel)
    assert dest.read_bytes() == b"existing verified destination"
    assert not downloader.part_path(dest).exists()


def test_http_error_and_refused_scheme(server, tmp_path: Path) -> None:
    base, _ = server
    with pytest.raises(downloader.DownloadError, match="HTTP 404"):
        downloader.download_verified(f"{base}/nope", tmp_path / "a", SHA)
    with pytest.raises(downloader.DownloadError, match="non-HTTPS"):
        downloader.download_verified("http://example.com/x", tmp_path / "a", SHA)
    assert list(tmp_path.iterdir()) == []


def test_concurrent_download_of_same_file_is_refused(server, tmp_path: Path) -> None:
    import fcntl
    base, _ = server
    dest = tmp_path / "f.bin"
    part = downloader.part_path(dest)
    fd = os.open(part, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(downloader.DownloadError, match="already being downloaded"):
            downloader.download_verified(f"{base}/model.bin", dest, SHA)
        assert part.exists()  # the other owner's file is left alone
        assert downloader.remove_stale_part(dest) is False
        assert part.exists()
    finally:
        os.close(fd)
    assert downloader.remove_stale_part(dest) is True  # owner gone: stale
    assert not part.exists()


def _tarball(path: Path, members: dict[str, bytes], top: str = "crispasr-linux-x86_64") -> None:
    with tarfile.open(path, "w:gz") as tf:
        d = tarfile.TarInfo(top)
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        tf.addfile(d)
        for name, data in members.items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))


def test_extract_into_fresh_dir_and_replace(tmp_path: Path) -> None:
    tar = tmp_path / "b.tar.gz"
    _tarball(tar, {"crispasr": b"#!/bin/sh\n", "libx.so": b"x"})
    dest = tmp_path / "engines" / "crispasr-0.8.42-avx512"
    downloader.extract_tarball(tar, dest, expect_file="crispasr")
    assert (dest / "crispasr").read_bytes() == b"#!/bin/sh\n"
    assert os.access(dest / "crispasr", os.X_OK)
    # Re-extract over an existing dir replaces it wholesale.
    (dest / "stale").write_text("old")
    downloader.extract_tarball(tar, dest, expect_file="crispasr")
    assert not (dest / "stale").exists()
    assert sorted(p.name for p in dest.parent.iterdir()) == [dest.name]


def test_extract_rejects_traversal_and_bad_layout(tmp_path: Path) -> None:
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tf:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(downloader.DownloadError):
        downloader.extract_tarball(evil, tmp_path / "out", expect_file="crispasr")
    assert not (tmp_path / "escape").exists()

    nobin = tmp_path / "nobin.tar.gz"
    _tarball(nobin, {"README": b"x"})
    with pytest.raises(downloader.DownloadError, match="crispasr missing"):
        downloader.extract_tarball(nobin, tmp_path / "out", expect_file="crispasr")
    assert not (tmp_path / "out").exists()
    # temp extraction dirs are cleaned up
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []
