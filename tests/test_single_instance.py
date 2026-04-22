"""Tests for the cross-process single-instance lock.

Two voxtype-tui processes against the same `~/.config/voxtype/config.toml`
silently lose updates — there's no fcntl lock around the read-modify-write
cycle. The lock guards against the AUR + dev-conda double-launch scenario.
"""
from __future__ import annotations

import os
from pathlib import Path

from voxtype_tui import single_instance


def test_acquire_succeeds_when_no_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / ".lock"
    result = single_instance.acquire(lock_path)
    assert result.acquired is True
    assert result.holder_pid is None
    assert result.fd is not None
    # PID is written so a contender can identify us.
    assert lock_path.read_text().strip() == str(os.getpid())
    os.close(result.fd)


def test_second_acquire_fails_with_holder_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / ".lock"
    first = single_instance.acquire(lock_path)
    assert first.acquired is True
    try:
        second = single_instance.acquire(lock_path)
        assert second.acquired is False
        assert second.holder_pid == os.getpid()
        assert second.fd is None
    finally:
        os.close(first.fd)


def test_acquire_after_release_succeeds(tmp_path: Path) -> None:
    """Closing the fd releases the flock — next acquire works."""
    lock_path = tmp_path / ".lock"
    first = single_instance.acquire(lock_path)
    assert first.acquired is True
    os.close(first.fd)

    second = single_instance.acquire(lock_path)
    assert second.acquired is True
    os.close(second.fd)


def test_lock_dir_is_created_if_missing(tmp_path: Path) -> None:
    lock_path = tmp_path / "nested" / "dir" / ".lock"
    result = single_instance.acquire(lock_path)
    assert result.acquired is True
    assert lock_path.parent.is_dir()
    os.close(result.fd)
