"""Tests for the per-engine Download button visibility + behavior.

The Models tab probes the user's voxtype binary at mount time to learn
which engines are compiled in. Engines that aren't compiled get a
disabled Download button (pressing it would fire a subprocess that
fails with a "rebuild with --features X" error), and a "(not
compiled)" label in the engine Select so users see why.

Whisper is always assumed compiled (fallback on probe failure). Custom
voxtype builds with every feature enabled get the full set.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from voxtype_tui import config, sidecar, voxtype_cli
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.models import ModelsPane

from .conftest import FIXTURES


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)
    from voxtype_tui import models as models_mod
    models_dir = tmp_path / "voxtype-models"
    models_dir.mkdir()
    monkeypatch.setattr(models_mod, "MODELS_DIR", models_dir)
    return cfg, side, models_dir


async def _switch_to_engine(pilot, app, engine: str) -> None:
    """Move the Models-tab engine Select to a specific engine."""
    await pilot.press("4")  # Models tab
    await pilot.pause()
    from textual.widgets import Select
    pane = app.query_one(ModelsPane)
    pane.query_one("#models-engine", Select).value = engine
    await pilot.pause()


# ---------------------------------------------------------------------------

async def test_whisper_download_button_enabled(tmp_env, monkeypatch):
    """Whisper is always compiled → button enabled → pressing it fires
    the existing `_run_download` subprocess flow."""
    cfg, side, _ = tmp_env
    monkeypatch.setattr(voxtype_cli, "compiled_engines", lambda timeout=5.0: {"whisper"})
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)

    download_calls: list[str] = []

    async def fake_download(self, name):
        download_calls.append(name)

    monkeypatch.setattr(ModelsPane, "_run_download", fake_download)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        from textual.widgets import Button
        btn = pane.query_one("#models-download", Button)
        assert btn.disabled is False
        pane._selected_model_name = lambda: "tiny.en"
        pane._action_download()
        for _ in range(5):
            await pilot.pause()
    assert download_calls == ["tiny.en"]


async def test_uncompiled_engine_download_button_disabled(tmp_env, monkeypatch):
    """Switching the Select to an uncompiled engine must disable the
    Download button. The user can't reach a misleading failure path."""
    cfg, side, _ = tmp_env
    monkeypatch.setattr(voxtype_cli, "compiled_engines", lambda timeout=5.0: {"whisper"})
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _switch_to_engine(pilot, app, "moonshine")
        pane = app.query_one(ModelsPane)
        from textual.widgets import Button
        btn = pane.query_one("#models-download", Button)
        assert btn.disabled is True


async def test_compiled_non_whisper_engine_unlocks_download(tmp_env, monkeypatch):
    """On a custom voxtype build with moonshine compiled in, switching
    to moonshine must enable the Download button and route through the
    same subprocess flow whisper uses."""
    cfg, side, _ = tmp_env
    monkeypatch.setattr(
        voxtype_cli, "compiled_engines",
        lambda timeout=5.0: {"whisper", "moonshine"},
    )
    download_calls: list[str] = []
    async def fake_download(self, name):
        download_calls.append(name)
    monkeypatch.setattr(ModelsPane, "_run_download", fake_download)

    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _switch_to_engine(pilot, app, "moonshine")
        pane = app.query_one(ModelsPane)
        from textual.widgets import Button
        assert pane.query_one("#models-download", Button).disabled is False
        pane._selected_model_name = lambda: "base"
        pane._action_download()
        for _ in range(5):
            await pilot.pause()
    assert download_calls == ["base"]


async def test_uncompiled_engine_label_marked_in_select(tmp_env, monkeypatch):
    """The engine Select's visible labels must flag uncompiled engines
    so the user understands why Download is unavailable."""
    cfg, side, _ = tmp_env
    monkeypatch.setattr(
        voxtype_cli, "compiled_engines",
        lambda timeout=5.0: {"whisper", "parakeet"},
    )
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        from textual.widgets import Select
        sel = pane.query_one("#models-engine", Select)
        labels = {str(label): value for label, value in sel._options}
        # Whisper + parakeet compiled: plain labels.
        assert any(k == "whisper" for k in labels)
        assert any(k == "parakeet" for k in labels)
        # Moonshine etc uncompiled: label has "(not compiled)" suffix.
        assert any(k == "moonshine (not compiled)" for k in labels)


async def test_probe_fallback_keeps_whisper_unlocked(tmp_env, monkeypatch):
    """If the `voxtype setup model` probe fails (binary missing,
    timeout, parse error), fallback guarantees whisper stays working."""
    cfg, side, _ = tmp_env
    def boom(timeout=5.0):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(voxtype_cli, "compiled_engines", boom)
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        from textual.widgets import Button
        assert pane.query_one("#models-download", Button).disabled is False


# ---------------------------------------------------------------------------
# compiled_engines parser unit tests
# ---------------------------------------------------------------------------

def test_compiled_engines_parses_mixed_output(monkeypatch):
    """Feed the parser a realistic voxtype output and verify only the
    engines whose section lacks the "(not available ...)" marker get
    reported as compiled."""
    fake_output = (
        "Voxtype Model Selection\n"
        "\n"
        "--- Whisper (OpenAI) ---\n"
        "  [ 1] tiny (75 MB)\n"
        "  [ 2] base (142 MB)\n"
        "\n"
        "--- Parakeet (NVIDIA) ---\n"
        "  (not available - rebuild with --features parakeet)\n"
        "\n"
        "--- Moonshine (MoonshineAI) ---\n"
        "  [ 3] base (237 MB)\n"
        "\n"
        "--- SenseVoice ---\n"
        "  (not available - rebuild with --features sensevoice)\n"
        "\n"
        "--- Paraformer ---\n"
        "  (not available - rebuild with --features paraformer)\n"
    )
    class FakeResult:
        stdout = fake_output
        returncode = 0
    import subprocess as _s
    monkeypatch.setattr(voxtype_cli, "shutil",
                        type("X", (), {"which": staticmethod(lambda n: "/usr/bin/voxtype")}))
    monkeypatch.setattr(_s, "run", lambda *a, **kw: FakeResult())
    monkeypatch.setattr(voxtype_cli, "subprocess", _s)
    got = voxtype_cli.compiled_engines(timeout=1.0)
    assert got == {"whisper", "moonshine"}


def test_compiled_engines_binary_missing(monkeypatch):
    """No voxtype on PATH → return whisper-only fallback."""
    monkeypatch.setattr(voxtype_cli, "shutil",
                        type("X", (), {"which": staticmethod(lambda n: None)}))
    assert voxtype_cli.compiled_engines() == {"whisper"}


def test_compiled_engines_timeout_fallback(monkeypatch):
    """Subprocess timeout → return whisper-only fallback."""
    import subprocess as _s
    monkeypatch.setattr(voxtype_cli, "shutil",
                        type("X", (), {"which": staticmethod(lambda n: "/usr/bin/voxtype")}))
    def raise_timeout(*a, **kw):
        raise _s.TimeoutExpired(cmd="voxtype", timeout=1)
    monkeypatch.setattr(_s, "run", raise_timeout)
    monkeypatch.setattr(voxtype_cli, "subprocess", _s)
    assert voxtype_cli.compiled_engines(timeout=1.0) == {"whisper"}


def test_compiled_engines_unparseable_output_fallback(monkeypatch):
    """If the output doesn't contain any known engine headers (e.g.
    Voxtype changed the format), fallback to whisper-only rather than
    greying everything."""
    class FakeResult:
        stdout = "Something totally different"
        returncode = 0
    import subprocess as _s
    monkeypatch.setattr(voxtype_cli, "shutil",
                        type("X", (), {"which": staticmethod(lambda n: "/usr/bin/voxtype")}))
    monkeypatch.setattr(_s, "run", lambda *a, **kw: FakeResult())
    monkeypatch.setattr(voxtype_cli, "subprocess", _s)
    assert voxtype_cli.compiled_engines(timeout=1.0) == {"whisper"}
