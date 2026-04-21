"""Proves the save flow doesn't freeze the event loop while voxtype validation
and systemctl calls are running. Without asyncio.to_thread wrappers, a sync
subprocess.run would block the UI (status poller, key input) for the duration.
"""
from __future__ import annotations

import asyncio
import inspect
import shutil
import time
from pathlib import Path

import pytest

from voxtype_tui import config, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.state import AppState
from .conftest import FIXTURES


def test_action_save_is_coroutine_function() -> None:
    """Structural guarantee: action_save must be async so subprocess work
    can be awaited without blocking the event loop."""
    assert inspect.iscoroutinefunction(VoxtypeTUI.action_save)


def test_state_save_async_exists() -> None:
    assert inspect.iscoroutinefunction(AppState.save_async)


def test_config_safe_save_async_exists() -> None:
    assert inspect.iscoroutinefunction(config.safe_save_async)


def test_voxtype_cli_async_wrappers_exist() -> None:
    assert inspect.iscoroutinefunction(voxtype_cli.is_daemon_active_async)
    assert inspect.iscoroutinefunction(voxtype_cli.restart_daemon_async)


async def test_save_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow-validate stub: block for 300ms inside the (sync) validator.
    While the save is in flight we should still be able to await tiny sleeps
    — proof the event loop is ticking. If the save were synchronous it would
    hold the loop for the full 300ms and the concurrent sleeps would run
    consecutively after it finishes."""
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)

    def slow_validate(p, timeout=10.0):
        time.sleep(0.3)
        return True, ""

    monkeypatch.setattr(config, "validate_with_voxtype", slow_validate)

    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.state.add_vocab("Testing")
        app.refresh_dirty()

        # Kick off save. Meanwhile, count how many concurrent tick-sleeps
        # complete — a blocking save would leave this at 0 or 1.
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        task = asyncio.create_task(ticker())
        start = time.monotonic()
        await app.action_save()
        elapsed = time.monotonic() - start
        task.cancel()

        # Save took the full ~300ms of blocked validation time...
        assert elapsed >= 0.25, f"save suspiciously fast — maybe stubbed wrong ({elapsed:.3f}s)"
        # ...but the event loop was free to tick during it (at 50Hz we expect
        # at least ~10 ticks in 300ms; allow slack for scheduler jitter).
        assert ticks >= 5, f"event loop blocked (ticks={ticks}, elapsed={elapsed:.3f}s)"

        assert app.state.dirty is False
