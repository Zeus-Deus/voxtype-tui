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
import time
from pathlib import Path

STATE_FILE = Path(f"/run/user/{os.getuid()}/voxtype/state")

# States the daemon writes to its state file once initialization is done.
# `systemctl restart voxtype` returns the moment the unit is `active`, but
# voxtype still has to load the model into memory (and init GPU) before it
# can serve. The state file appearing with one of these values is the
# honest signal that the daemon is actually ready.
DAEMON_READY_STATES: tuple[str, ...] = ("idle", "recording", "transcribing")


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


async def wait_for_daemon_ready_async(
    timeout: float = 20.0,
    poll_interval: float = 0.15,
) -> bool:
    """Poll the daemon state file until it reports a ready state, or give
    up after `timeout` seconds. Returns True on ready, False on timeout.

    `restart_daemon` is the systemctl-level signal — it goes True the moment
    the unit is `active`. That's well before the daemon has finished
    loading the Whisper model on a typical config, so a UI that flips to
    "Ready" on systemctl's word is lying to the user. This helper closes
    the gap by waiting for the state file the daemon itself writes once
    the model is loaded.
    """
    deadline = time.monotonic() + timeout
    while True:
        if read_state() in DAEMON_READY_STATES:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_interval)


# Engines Voxtype knows about. The subset that's actually compiled into
# the installed binary is detected at runtime by `compiled_engines()`
# below — upstream AUR builds (`voxtype-bin`) ship whisper-only; custom
# `cargo build --features X,Y,Z` unlocks others.
_KNOWN_ENGINES: tuple[str, ...] = (
    "whisper", "parakeet", "moonshine", "sensevoice",
    "paraformer", "dolphin", "omnilingual",
)


# Strings we match against the output of `voxtype setup model`. Regex-free
# substring checks — the output is plain ASCII with ANSI colors, and
# a .startswith() match on the header line is sufficient to identify
# each engine's section.
_ENGINE_HEADERS: dict[str, str] = {
    "whisper": "--- Whisper",
    "parakeet": "--- Parakeet",
    "moonshine": "--- Moonshine",
    "sensevoice": "--- SenseVoice",
    "paraformer": "--- Paraformer",
    "dolphin": "--- Dolphin",
    "omnilingual": "--- Omnilingual",
}


def compiled_engines(timeout: float = 5.0) -> set[str]:
    """Return the set of engine names compiled into the installed
    Voxtype binary.

    Works by running ``voxtype setup model`` with stdin closed and
    scanning the output for ``(not available - rebuild with
    --features X)`` markers. Engines whose section *lacks* that marker
    are considered compiled-in.

    Fallback: on any error (binary missing, subprocess timeout,
    unparseable output), return ``{"whisper"}`` — the most common
    shape on AUR/precompiled installs. Preserving the download button
    for whisper matters more than being strictly accurate on error.
    """
    if shutil.which("voxtype") is None:
        return {"whisper"}
    try:
        result = subprocess.run(
            ["voxtype", "setup", "model"],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"whisper"}

    # Strip ANSI color codes for matching — the "(not available ...)"
    # marker is wrapped in grey.
    import re
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    lines = plain.splitlines()

    compiled: set[str] = set()
    current_engine: str | None = None
    for line in lines:
        stripped = line.strip()
        matched_engine: str | None = None
        for engine, header in _ENGINE_HEADERS.items():
            if stripped.startswith(header):
                matched_engine = engine
                break
        if matched_engine is not None:
            # New section header — the previous engine either reported
            # "(not available ...)" inside or stayed compiled.
            if current_engine is not None:
                compiled.add(current_engine)
            current_engine = matched_engine
            continue
        if current_engine is not None and "(not available" in stripped:
            # This engine's section declared itself uncompiled.
            current_engine = None
    if current_engine is not None:
        compiled.add(current_engine)

    # Defensive: if parsing yielded nothing (output format changed),
    # fall back to whisper-only rather than greying everything out.
    return compiled or {"whisper"}


async def compiled_engines_async(timeout: float = 5.0) -> set[str]:
    return await asyncio.to_thread(compiled_engines, timeout)
