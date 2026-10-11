"""Models / Settings tabs with the extra models and the Qwen3-ASR engine,
plus the download child's SIGTERM cancel as a real subprocess."""
from __future__ import annotations

import hashlib
import http.server
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, Select

from voxtype_tui import asr_server, voxtype_cli
from voxtype_tui import extra_models as xm
from voxtype_tui.app import VoxtypeTUI
from voxtype_tui.extra_models import ENGINE_QWEN3
from voxtype_tui.models import ModelsPane
from voxtype_tui.settings import SettingsPane

from .conftest import FIXTURES

CPU512 = xm.Accel(True, "whisper-avx512", False, "avx512", 6)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.toml"
    side = tmp_path / "metadata.json"
    shutil.copy(FIXTURES / "stock.toml", cfg)
    md = tmp_path / "models"
    md.mkdir()
    for f in ("ggml-base.en.bin", "ggml-large-v3-turbo-q5_0.bin", "qwen3-asr-0.6b-q4_k.gguf"):
        (md / f).write_bytes(b"x" * 1000)
    b = xm.crispasr_binary("avx512")
    b.parent.mkdir(parents=True)
    b.write_text("#!/bin/sh\n")
    b.chmod(0o755)
    from voxtype_tui import models as models_mod
    monkeypatch.setattr(models_mod, "MODELS_DIR", md)
    monkeypatch.setenv("VOXTYPE_MODELS_DIR", str(md))

    async def _inactive():
        return False
    monkeypatch.setattr(voxtype_cli, "is_daemon_active", lambda: False)
    monkeypatch.setattr(voxtype_cli, "is_daemon_active_async", _inactive)
    monkeypatch.setattr(voxtype_cli, "compiled_engines", lambda timeout=5.0: {"whisper"})
    return cfg, side, md


def _rows(pane: ModelsPane) -> dict[str, list[str]]:
    t = pane.query_one(DataTable)
    return {str(t.get_row_at(i)[0]): [str(c) for c in t.get_row_at(i)] for i in range(t.row_count)}


def _cursor_to(pane: ModelsPane, name: str) -> None:
    t = pane.query_one(DataTable)
    for i in range(t.row_count):
        if str(t.get_row_at(i)[0]) == name:
            t.move_cursor(row=i)
            return
    raise AssertionError(name)


async def test_cpu_box_shows_gpu_only_reason_and_disables_download(env, monkeypatch):
    cfg, side, md = env
    monkeypatch.setattr(xm, "_ACCEL_CACHE", CPU512)
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.press("4")
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        rows = _rows(pane)
        assert rows["large-v3-turbo-q5_0"][2] == "downloaded · needs GPU acceleration"
        assert rows["large-v3-turbo-q8_0"][2] == "needs GPU acceleration"
        _cursor_to(pane, "large-v3-turbo-q8_0")
        await pilot.pause()
        assert pane.query_one("#models-download", Button).disabled
        _cursor_to(pane, "base.en")
        await pilot.pause()
        assert not pane.query_one("#models-download", Button).disabled
        # set-active on the GPU-only model is refused with a warning
        notes: list[tuple[str, str]] = []
        orig = app.notify
        app.notify = lambda m, *a, severity="information", **k: (
            notes.append((severity, str(m))), orig(m, *a, severity=severity, **k))[1]
        _cursor_to(pane, "large-v3-turbo-q5_0")
        pane._action_set_active()
        await pilot.pause()
        assert notes[-1][0] == "warning" and "needs GPU acceleration" in notes[-1][1]
        assert str(app.state.doc["whisper"]["model"]) == "base.en"


async def test_gpu_box_sets_quantized_model_as_bare_filename(env):
    cfg, side, md = env
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.press("4")
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        _cursor_to(pane, "large-v3-turbo-q5_0")
        pane._action_set_active()
        await pilot.pause()
        assert str(app.state.doc["whisper"]["model"]) == "ggml-large-v3-turbo-q5_0.bin"
        assert _rows(pane)["large-v3-turbo-q5_0"][3] == "●"
        settings = app.query_one(SettingsPane)
        assert settings.query_one("#settings-model", Select).value == "large-v3-turbo-q5_0"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert 'model = "ggml-large-v3-turbo-q5_0.bin"' in cfg.read_text()


async def test_qwen3_engine_listed_and_activated_from_models_tab(env, monkeypatch):
    cfg, side, md = env
    monkeypatch.setattr(xm, "_ACCEL_CACHE", CPU512)
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.press("4")
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        sel = pane.query_one("#models-engine", Select)
        labels = {v: str(l) for l, v in sel._options if v is not Select.BLANK}
        assert labels[ENGINE_QWEN3] == "Qwen3-ASR"
        sel.value = ENGINE_QWEN3
        await pilot.pause()
        rows = _rows(pane)
        assert rows["0.6b-q4_k"][2] == "downloaded" and rows["0.6b-q8_0"][2] == "—"
        _cursor_to(pane, "0.6b-q4_k")
        pane._action_set_active()
        await pilot.pause()
        assert asr_server.is_qwen3_active(app.state.doc, app.state.sc)
        assert _rows(pane)["0.6b-q4_k"][3] == "●"
        settings = app.query_one(SettingsPane)
        assert settings.query_one("#settings-engine", Select).value == ENGINE_QWEN3
        # delete refused while active
        notes: list[tuple[str, str]] = []
        orig = app.notify
        app.notify = lambda m, *a, severity="information", **k: (
            notes.append((severity, str(m))), orig(m, *a, severity=severity, **k))[1]
        _cursor_to(pane, "0.6b-q4_k")
        pane._action_delete()
        await pilot.pause()
        assert notes[-1][0] == "warning" and "active" in notes[-1][1]
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.state.asr_problems == []
    assert asr_server.unit_path().exists()
    assert "qwen3_asr" in side.read_text()

    # Reopen: still Qwen3; switch back to whisper from the Models tab.
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.press("4")
        await pilot.pause()
        pane = app.query_one(ModelsPane)
        assert pane.query_one("#models-engine", Select).value == ENGINE_QWEN3
        pane.query_one("#models-engine", Select).value = "whisper"
        await pilot.pause()
        assert "●" not in [r[3] for r in _rows(pane).values()]  # whisper not live
        _cursor_to(pane, "base.en")
        pane._action_set_active()
        await pilot.pause()
        assert not asr_server.is_qwen3_active(app.state.doc, app.state.sc)
        assert "mode" not in app.state.doc["whisper"]
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert not asr_server.unit_path().exists()


async def test_settings_engine_select_switches_to_qwen3_and_back(env, monkeypatch):
    cfg, side, md = env
    monkeypatch.setattr(xm, "_ACCEL_CACHE", CPU512)
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.pause()
        pane = app.query_one(SettingsPane)
        eng = pane.query_one("#settings-engine", Select)
        eng.value = ENGINE_QWEN3
        await pilot.pause()
        assert asr_server.is_qwen3_active(app.state.doc, app.state.sc)
        model = pane.query_one("#settings-model", Select)
        assert model.value == "0.6b-q4_k"
        opts = {v: str(l) for l, v in model._options if v is not Select.BLANK}
        assert opts["0.6b-q8_0"] == "0.6b-q8_0 (not downloaded)"
        eng.value = "whisper"
        await pilot.pause()
        assert not asr_server.is_qwen3_active(app.state.doc, app.state.sc)
        assert str(app.state.doc["engine"]) == "whisper"


async def test_settings_qwen3_refused_without_download(env, monkeypatch):
    cfg, side, md = env
    (md / "qwen3-asr-0.6b-q4_k.gguf").unlink()
    app = VoxtypeTUI(config_path=cfg, sidecar_path=side)
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.pause()
        pane = app.query_one(SettingsPane)
        notes: list[tuple[str, str]] = []
        orig = app.notify
        app.notify = lambda m, *a, severity="information", **k: (
            notes.append((severity, str(m))), orig(m, *a, severity=severity, **k))[1]
        pane.query_one("#settings-engine", Select).value = ENGINE_QWEN3
        await pilot.pause()
        await pilot.pause()
        assert notes and notes[-1][0] == "warning" and "download" in notes[-1][1]
        assert pane.query_one("#settings-engine", Select).value == "whisper"
        assert not app.state.dirty


# --- download child: real subprocess + SIGTERM --------------------------------


class _Slow(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        try:
            for i in range(0, len(self.body), 65536):
                self.wfile.write(self.body[i:i + 65536])
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


def test_download_child_sigterm_removes_part(tmp_path: Path) -> None:
    _Slow.body = os.urandom(8 * 1024 * 1024)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/m.gguf"
    sha = hashlib.sha256(_Slow.body).hexdigest()
    driver = textwrap.dedent(f"""
        import sys
        from voxtype_tui import extra_models as xm
        m = xm.ExtraModel("qwen3-asr", "0.6b-q4_k", "qwen3-asr-0.6b-q4_k.gguf",
                          {url!r}, {sha!r}, {len(_Slow.body)})
        xm.EXTRA_MODELS = (m,)
        xm.build_installed = lambda v: True
        xm._ACCEL_CACHE = xm.Accel(True, "whisper-avx512", False, "avx512", 2)
        sys.exit(xm.main(["download", "qwen3-asr", "0.6b-q4_k"]))
    """)
    md = tmp_path / "models"
    env = dict(os.environ, VOXTYPE_MODELS_DIR=str(md),
               PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    proc = subprocess.Popen([sys.executable, "-c", driver], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, bufsize=0)
    part = md / "qwen3-asr-0.6b-q4_k.gguf.part"
    try:
        deadline = time.monotonic() + 15
        while not (part.exists() and part.stat().st_size > 0):
            assert time.monotonic() < deadline, "download never started"
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
        httpd.shutdown()
        httpd.server_close()
    text = out.decode()
    assert proc.returncode == 130, text
    assert "%" in text and "Cancelled; partial file removed" in text
    assert not part.exists()
    assert not (md / "qwen3-asr-0.6b-q4_k.gguf").exists()
