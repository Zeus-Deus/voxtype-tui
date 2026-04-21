"""Thin wrappers around the voxtype CLI and the running daemon.

Sync versions (`is_daemon_active`, `restart_daemon`) are called from scripts
and tests. Async versions (`*_async`) wrap them via `asyncio.to_thread` so the
Textual event loop stays responsive while systemctl is doing its thing.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

STATE_FILE = Path(f"/run/user/{os.getuid()}/voxtype/state")


def read_state() -> str | None:
    """Returns the daemon state word (idle/recording/transcribing) or None
    when the state file is missing or unreadable."""
    try:
        return STATE_FILE.read_text().strip() or None
    except OSError:
        return None


def is_daemon_active() -> bool:
    """True when `systemctl --user is-active voxtype` reports 'active'."""
    if shutil.which("systemctl") is None:
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "voxtype"],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.stdout.strip() == "active"


def restart_daemon() -> tuple[bool, str]:
    """Returns (ok, message)."""
    if shutil.which("systemctl") is None:
        return False, "systemctl not available"
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "voxtype"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, "restart timed out"
    except OSError as e:
        return False, str(e)
    if result.returncode == 0:
        return True, "voxtype restarted"
    return False, (result.stderr or result.stdout).strip() or "restart failed"


async def is_daemon_active_async() -> bool:
    return await asyncio.to_thread(is_daemon_active)


async def restart_daemon_async() -> tuple[bool, str]:
    return await asyncio.to_thread(restart_daemon)
