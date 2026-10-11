"""Extra models + Qwen3-ASR engine: catalog/name mapping, variant
selection, availability, download orchestration (local server), unit /
env rendering, engine switch + restore, refusal paths, save reconcile."""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest

from voxtype_tui import asr_server, downloader, sidecar, state
from voxtype_tui import extra_models as xm
from voxtype_tui.extra_models import ENGINE_QWEN3, RefusedError

from .conftest import FIXTURES

# Captured at collection time, before the autouse conftest stub replaces it.
_REAL_VOXTYPE_VARIANT = xm._voxtype_variant

GPU = xm.Accel(True, "whisper-vulkan", True, "avx512", 6)
CPU512 = xm.Accel(True, "whisper-avx512", False, "avx512", 6)
CPU2 = xm.Accel(True, "whisper-avx2", False, "avx2", 4)
OLD = xm.Accel(True, "whisper-baseline", False, "legacy", 2)
NOVOX = xm.Accel(False, None, False, "avx512", 6)


# --- catalog / names ------------------------------------------------------


def test_catalog_pins_are_well_formed() -> None:
    for m in xm.EXTRA_MODELS:
        assert len(m.sha256) == 64 and int(m.sha256, 16) >= 0
        assert m.url.startswith("https://huggingface.co/") and m.url.endswith(m.filename)
        assert m.size_bytes > 100 * 1024 * 1024
    for b in xm.CRISPASR_BUILDS.values():
        assert len(b.sha256) == 64
        assert b.url == (
            f"https://github.com/CrispStrobe/CrispASR/releases/download/v0.8.42/{b.asset}"
        )
    assert {m.name for m in xm.QWEN3_MODELS} == {"0.6b-q8_0", "0.6b-q4_k"}
    assert "large-v3-turbo-q5_0" in xm.extra_names("whisper")


@pytest.mark.parametrize("name,value", [
    ("large-v3-turbo-q5_0", "ggml-large-v3-turbo-q5_0.bin"),
    ("large-v3-turbo-q8_0", "ggml-large-v3-turbo-q8_0.bin"),
    ("base.en", "base.en"),
])
def test_config_value_roundtrip(name: str, value: str) -> None:
    assert xm.config_model_value("whisper", name) == value
    assert xm.catalog_model_name("whisper", value) == name


def test_catalog_name_leaves_paths_and_other_engines_alone() -> None:
    assert xm.catalog_model_name("whisper", "/x/ggml-foo.bin") == "/x/ggml-foo.bin"
    assert xm.catalog_model_name("whisper", "custom.bin") == "custom.bin"
    assert xm.catalog_model_name("parakeet", "ggml-x.bin") == "ggml-x.bin"
    assert xm.config_model_value(ENGINE_QWEN3, "0.6b-q4_k") == "0.6b-q4_k"


def test_models_module_integration(tmp_path: Path, monkeypatch) -> None:
    from voxtype_tui import models
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path)
    (tmp_path / "ggml-large-v3-turbo-q5_0.bin").write_bytes(b"x")
    (tmp_path / "qwen3-asr-0.6b-q4_k.gguf").write_bytes(b"x")
    (tmp_path / "qwen3-asr-0.6b-q8_0.gguf.part").write_bytes(b"x")
    assert "large-v3-turbo-q5_0" in models.scan_downloaded("whisper")
    assert models.scan_downloaded(ENGINE_QWEN3) == {"0.6b-q4_k": 1}
    # bare-filename config value resolves to the same file
    assert models.model_file_path("whisper", "ggml-large-v3-turbo-q5_0.bin") == (
        tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    )
    assert models.is_model_installed(ENGINE_QWEN3, "0.6b-q4_k")
    assert not models.is_model_installed(ENGINE_QWEN3, "0.6b-q8_0")
    assert [m.name for m in models.MODEL_CATALOG[ENGINE_QWEN3]] == ["0.6b-q8_0", "0.6b-q4_k"]


def test_native_path_templates_match_models_module(tmp_path: Path) -> None:
    from voxtype_tui import models
    for engine, tmpl in asr_server.NATIVE_PATH_TEMPLATES.items():
        assert models.model_file_path(engine, "n", models_dir=tmp_path) == (
            tmp_path / tmpl.format(name="n")
        )


def test_headless_modules_do_not_import_textual() -> None:
    code = ("import sys; import voxtype_tui.extra_models, voxtype_tui.asr_server, "
            "voxtype_tui.downloader, voxtype_tui.state; "
            "print('textual' in sys.modules)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=Path(__file__).resolve().parent.parent)
    assert r.stdout.strip() == "False", r.stderr


# --- variant / availability ----------------------------------------------


@pytest.mark.parametrize("accel,variant", [
    (GPU, "vulkan"), (CPU512, "avx512"), (CPU2, "generic"), (OLD, "cpu-legacy"),
])
def test_crispasr_variant(accel, variant) -> None:
    assert xm.crispasr_variant(accel) == variant


def test_availability_reasons() -> None:
    assert xm.availability("whisper", "large-v3-turbo-q5_0", GPU) == (True, None)
    assert xm.availability("whisper", "large-v3-turbo-q5_0", CPU512) == (
        False, xm.REASON_NEEDS_GPU)
    assert xm.availability(ENGINE_QWEN3, "0.6b-q4_k", CPU512) == (True, None)
    assert xm.availability(ENGINE_QWEN3, "0.6b-q4_k", NOVOX) == (False, xm.REASON_NO_VOXTYPE)
    assert xm.availability("whisper", "base.en", CPU512) == (True, None)


def test_list_extra_models_rows(tmp_path: Path) -> None:
    rows = xm.list_extra_models(models_dir=tmp_path, accel=CPU512)
    q5 = next(r for r in rows if r["name"] == "large-v3-turbo-q5_0")
    assert q5["available"] is False and q5["disabled_reason"] == "needs GPU acceleration"
    assert q5["engine_label"] == "whisper"
    q4 = next(r for r in rows if r["name"] == "0.6b-q4_k")
    assert q4["engine_label"] == "Qwen3-ASR" and q4["available"] and not q4["downloaded"]


def test_quantized_warning() -> None:
    import tomlkit
    doc = tomlkit.parse('[whisper]\nmodel = "ggml-large-v3-turbo-q5_0.bin"\n')
    assert "needs GPU acceleration" in xm.quantized_whisper_warning(doc, CPU512)
    assert xm.quantized_whisper_warning(doc, GPU) is None
    doc2 = tomlkit.parse('[whisper]\nmodel = "base.en"\n')
    assert xm.quantized_whisper_warning(doc2, CPU512) is None


def test_detect_accel_parses_voxtype_json(monkeypatch) -> None:
    # conftest stubs the probe; exercise the real one (captured at import).
    monkeypatch.setattr(xm, "_voxtype_variant", _REAL_VOXTYPE_VARIANT)
    monkeypatch.setattr(xm.shutil, "which", lambda n: "/usr/bin/voxtype")

    class R:
        returncode = 0
        stdout = json.dumps({"active_variant": "whisper-avx512", "running_variant": None})
    monkeypatch.setattr(xm.subprocess, "run", lambda *a, **k: R())
    a = xm.detect_accel(refresh=True)
    assert a.voxtype_variant == "whisper-avx512" and a.voxtype_gpu is False
    R.stdout = json.dumps({"active_variant": "whisper-avx512",
                           "running_variant": "whisper-vulkan"})
    assert xm.detect_accel(refresh=True).voxtype_gpu is True


def test_physical_cores_counts_unique_core_ids(monkeypatch, tmp_path: Path) -> None:
    text = "".join(
        f"processor\t: {i}\nphysical id\t: 0\ncore id\t\t: {i % 4}\n\n" for i in range(8)
    )
    orig = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k:
                        text if str(self) == "/proc/cpuinfo" else orig(self, *a, **k))
    assert xm.physical_cores() == 4


# --- download orchestration (local server) ---------------------------------


class _H(http.server.BaseHTTPRequestHandler):
    routes: dict[str, bytes] = {}

    def do_GET(self):  # noqa: N802
        body = self.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _tar(top: str, files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        d = tarfile.TarInfo(top)
        d.type = tarfile.DIRTYPE
        tf.addfile(d)
        for n, data in files.items():
            i = tarfile.TarInfo(f"{top}/{n}")
            i.size = len(data)
            i.mode = 0o755
            tf.addfile(i, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def fake_hub(monkeypatch):
    """Re-point the Qwen3 q4_k model + avx512 build at a local server."""
    model = os.urandom(300_000)
    tar = _tar("crispasr-linux-x86_64-avx512", {"crispasr": b"#!/bin/sh\nexit 0\n"})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    _H.routes = {"/m.gguf": model, "/b.tar.gz": tar}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    m = xm.ExtraModel(ENGINE_QWEN3, "0.6b-q4_k", "qwen3-asr-0.6b-q4_k.gguf",
                      f"{base}/m.gguf", hashlib.sha256(model).hexdigest(), len(model))
    monkeypatch.setattr(xm, "QWEN3_MODELS", (xm.QWEN3_MODELS[0], m))
    monkeypatch.setattr(xm, "EXTRA_MODELS", xm.EXTRA_WHISPER_MODELS + xm.QWEN3_MODELS)
    b = xm.CrispBuild("avx512", "b.tar.gz", len(tar), hashlib.sha256(tar).hexdigest())
    monkeypatch.setattr(xm.CrispBuild, "url", property(lambda self: f"{base}/{self.asset}"))
    monkeypatch.setitem(xm.CRISPASR_BUILDS, "avx512", b)
    yield model, tar
    httpd.shutdown()
    httpd.server_close()


def test_qwen3_download_fetches_build_and_model(fake_hub, tmp_path: Path) -> None:
    pct: list[float] = []
    out = xm.download(ENGINE_QWEN3, "0.6b-q4_k", models_dir=tmp_path,
                      progress=lambda p, _l: pct.append(p), accel=CPU512)
    assert out == tmp_path / "qwen3-asr-0.6b-q4_k.gguf"
    assert xm.build_installed("avx512")
    assert xm.build_dir("avx512").name == "crispasr-0.8.42-avx512"
    assert pct == sorted(pct) and pct[-1] == 100.0
    assert not (xm.engines_dir() / ".downloads" / "b.tar.gz").exists()
    # second run is a no-op (already present, pinned size)
    pct.clear()
    xm.download(ENGINE_QWEN3, "0.6b-q4_k", models_dir=tmp_path,
                progress=lambda p, _l: pct.append(p), accel=CPU512)
    assert pct == [100.0]


def test_qwen3_download_checksum_mismatch_leaves_nothing(fake_hub, tmp_path, monkeypatch):
    bad = xm.ExtraModel(ENGINE_QWEN3, "0.6b-q4_k", "qwen3-asr-0.6b-q4_k.gguf",
                        xm.QWEN3_MODELS[1].url, "f" * 64, xm.QWEN3_MODELS[1].size_bytes)
    monkeypatch.setattr(xm, "EXTRA_MODELS", xm.EXTRA_WHISPER_MODELS + (xm.QWEN3_MODELS[0], bad))
    with pytest.raises(downloader.ChecksumError):
        xm.download(ENGINE_QWEN3, "0.6b-q4_k", models_dir=tmp_path, accel=CPU512)
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_download_refuses_gpu_only_on_cpu(tmp_path: Path) -> None:
    with pytest.raises(RefusedError, match="needs GPU"):
        xm.download("whisper", "large-v3-turbo-q5_0", models_dir=tmp_path, accel=CPU512)
    with pytest.raises(RefusedError, match="not an extra"):
        xm.download("whisper", "base.en", models_dir=tmp_path, accel=GPU)


def test_cli_download_child_streams_progress_and_cancel_cleans(fake_hub, tmp_path, monkeypatch):
    """The plugin runs `python -m voxtype_tui.extra_models download …` like
    `voxtype setup --download`: `NN.N%` frames on stdout, exit 0."""
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setenv("VOXTYPE_MODELS_DIR", str(tmp_path))
    xm._ACCEL_CACHE = CPU512
    assert xm.main(["download", ENGINE_QWEN3, "0.6b-q4_k"]) == 0
    text = out.getvalue()
    assert "100.0%" in text and "Verified sha256" in text
    from voxtype_tui.models import parse_percent
    assert parse_percent(text.splitlines()[-2]) == 100.0
    assert xm.download_argv(ENGINE_QWEN3, "0.6b-q4_k")[1:3] == ["-m", "voxtype_tui.extra_models"]
    assert xm.download_argv("whisper", "base.en")[:3] == ["voxtype", "setup", "--download"]


def test_delete_last_qwen3_removes_engine_build(tmp_path: Path) -> None:
    (tmp_path / "qwen3-asr-0.6b-q4_k.gguf").write_bytes(b"x" * 10)
    (tmp_path / "qwen3-asr-0.6b-q8_0.gguf").write_bytes(b"x" * 10)
    xm.crispasr_binary("avx512").parent.mkdir(parents=True)
    xm.crispasr_binary("avx512").write_text("x")
    assert xm.delete(ENGINE_QWEN3, "0.6b-q4_k", models_dir=tmp_path) == 10
    assert xm.build_dir("avx512").exists()  # q8_0 still there
    xm.delete(ENGINE_QWEN3, "0.6b-q8_0", models_dir=tmp_path)
    assert not xm.build_dir("avx512").exists()
    with pytest.raises(RefusedError, match="nothing to delete"):
        xm.delete(ENGINE_QWEN3, "0.6b-q8_0", models_dir=tmp_path)


# --- engine switch / restore ------------------------------------------------


@pytest.fixture
def qenv(tmp_path: Path):
    """Loaded AppState on the stock fixture + downloaded q4_k + avx512 build."""
    cfg = tmp_path / "config.toml"
    shutil.copy(FIXTURES / "stock.toml", cfg)
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "qwen3-asr-0.6b-q4_k.gguf").write_bytes(b"x" * 10)
    (models_dir / "ggml-base.en.bin").write_bytes(b"x" * 10)
    (models_dir / "ggml-large-v3-turbo-q5_0.bin").write_bytes(b"x" * 10)
    b = xm.crispasr_binary("avx512")
    b.parent.mkdir(parents=True)
    b.write_text("#!/bin/sh\n")
    b.chmod(0o755)
    st = state.AppState.load(cfg, tmp_path / "metadata.json")
    return st, models_dir


def _installed(models_dir: Path):
    return lambda e, n: asr_server.native_model_installed(e, n, models_dir)


def test_activate_qwen3_writes_remote_config_and_marker(qenv) -> None:
    st, md = qenv
    st.set_setting("whisper.mode", "local")
    info = asr_server.set_active_model(st, ENGINE_QWEN3, "0.6b-q4_k", models_dir=md,
                                       accel=CPU512, port=45678)
    assert info["variant"] == "avx512" and info["endpoint"] == "http://127.0.0.1:45678"
    w = st.doc["whisper"]
    assert (str(st.doc["engine"]), w["mode"], w["remote_endpoint"], w["remote_model"],
            int(w["remote_timeout_secs"])) == (
        "whisper", "remote", "http://127.0.0.1:45678", "qwen3-asr", 60)
    assert str(w["model"]) == "base.en"  # untouched, restored target
    assert asr_server.is_qwen3_active(st.doc, st.sc)
    assert asr_server.active_engine_model(st.doc, st.sc) == (ENGINE_QWEN3, "0.6b-q4_k")
    prev = st.sc.qwen3_asr["previous_whisper"]
    assert prev["mode"] == "local" and prev["remote_endpoint"] is None


def test_switch_back_restores_previous_whisper_settings(qenv) -> None:
    st, md = qenv
    st.set_setting("whisper.mode", "local")
    asr_server.set_active_model(st, ENGINE_QWEN3, "0.6b-q4_k", models_dir=md, accel=CPU512)
    asr_server.set_active_model(st, "whisper", "large-v3-turbo-q5_0", models_dir=md,
                                accel=GPU, is_installed=_installed(md))
    w = st.doc["whisper"]
    assert w["mode"] == "local"
    assert "remote_endpoint" not in w and "remote_model" not in w
    assert str(w["model"]) == "ggml-large-v3-turbo-q5_0.bin"
    assert asr_server.active_engine_model(st.doc, st.sc) == ("whisper", "large-v3-turbo-q5_0")
    assert st.sc.qwen3_asr["active"] is False


def test_user_remote_server_survives_qwen3_roundtrip(qenv) -> None:
    st, md = qenv
    st.set_setting("whisper.mode", "remote")
    st.set_setting("whisper.remote_endpoint", "https://asr.example")
    st.set_setting("whisper.remote_model", "whisper-1")
    # not marked as Qwen3: user remote stays "whisper"
    assert asr_server.active_engine_model(st.doc, st.sc)[0] == "whisper"
    asr_server.activate_qwen3(st, "0.6b-q4_k", models_dir=md, accel=CPU512)
    asr_server.deactivate_qwen3(st)
    w = st.doc["whisper"]
    assert (w["mode"], w["remote_endpoint"], w["remote_model"]) == (
        "remote", "https://asr.example", "whisper-1")


def test_marker_without_matching_endpoint_is_not_qwen3(qenv) -> None:
    st, md = qenv
    asr_server.activate_qwen3(st, "0.6b-q4_k", models_dir=md, accel=CPU512, port=40001)
    st.set_setting("whisper.remote_endpoint", "http://127.0.0.1:40002")  # hand edit
    assert not asr_server.is_qwen3_active(st.doc, st.sc)
    assert asr_server.active_engine_model(st.doc, st.sc)[0] == "whisper"
    assert any("no longer points" in w for w in asr_server.load_warnings(st.doc, st.sc))


def test_model_switch_within_qwen3_keeps_original_snapshot(qenv) -> None:
    st, md = qenv
    (md / "qwen3-asr-0.6b-q8_0.gguf").write_bytes(b"x")
    asr_server.activate_qwen3(st, "0.6b-q4_k", models_dir=md, accel=CPU512, port=40010)
    asr_server.activate_qwen3(st, "0.6b-q8_0", models_dir=md, accel=CPU512)
    assert st.sc.qwen3_asr["port"] == 40010  # stable endpoint
    assert st.sc.qwen3_asr["previous_whisper"]["mode"] is None
    asr_server.deactivate_qwen3(st)
    assert "mode" not in st.doc["whisper"]


def test_refusals(qenv) -> None:
    st, md = qenv
    with pytest.raises(RefusedError, match="isn't downloaded"):
        asr_server.set_active_model(st, ENGINE_QWEN3, "0.6b-q8_0", models_dir=md, accel=CPU512)
    with pytest.raises(RefusedError, match="needs GPU"):
        asr_server.set_active_model(st, "whisper", "large-v3-turbo-q5_0", models_dir=md,
                                    accel=CPU512)
    with pytest.raises(RefusedError, match="isn't downloaded"):
        asr_server.set_active_model(st, "whisper", "large-v3", models_dir=md, accel=GPU,
                                    is_installed=_installed(md))
    shutil.rmtree(xm.build_dir("avx512"))
    with pytest.raises(RefusedError, match="CrispASR"):
        asr_server.set_active_model(st, ENGINE_QWEN3, "0.6b-q4_k", models_dir=md, accel=CPU512)
    assert not st.dirty


def test_delete_refused_while_active(qenv) -> None:
    st, md = qenv
    asr_server.activate_qwen3(st, "0.6b-q4_k", models_dir=md, accel=CPU512)
    with pytest.raises(RefusedError, match="active Qwen3-ASR model"):
        xm.delete(ENGINE_QWEN3, "0.6b-q4_k", doc=st.doc, sc=st.sc, models_dir=md)
    assert (md / "qwen3-asr-0.6b-q4_k.gguf").exists()


def test_port_taken_picks_another(qenv) -> None:
    import socket
    st, md = qenv
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    taken = s.getsockname()[1]
    try:
        st.sc.qwen3_asr = {"active": False, "port": taken}
        info = asr_server.activate_qwen3(st, "0.6b-q4_k", models_dir=md, accel=CPU512)
        assert info["port"] != taken
    finally:
        s.close()


# --- rendering ---------------------------------------------------------------


def test_render_env_escapes_and_roundtrips() -> None:
    phrases = ["Codemux", "Claude Code", 'say "hi"', "a,b", "$HOME", "back\\slash", "Codemux"]
    text = asr_server.render_env(phrases)
    assert text.splitlines()[1].startswith('VOXTYPE_ASR_HOTWORDS="')
    assert "\\$HOME" in text
    assert asr_server.parse_env_value(text) == (
        'Codemux,Claude Code,say "hi",a b,$HOME,back\\slash')
    assert asr_server.parse_env_value(asr_server.render_env([])) == ""


def _spec(gpu: bool, tmp: Path, env=()) -> asr_server.ServerSpec:
    return asr_server.ServerSpec(
        binary=tmp / "eng dir" / "crispasr", model=tmp / "m%.gguf", port=41234,
        threads=6, language="en", gpu=gpu, env=tuple(env),
    )


def test_render_unit_cpu(tmp_path: Path) -> None:
    unit = asr_server.render_unit(_spec(False, tmp_path), tmp_path / "q.env")
    for line in ("PartOf=voxtype.service", "Before=voxtype.service",
                 "WantedBy=voxtype.service", "Restart=on-failure"):
        assert line in unit.splitlines()
    env = asr_server.render_env(["Codemux", "Hyprland"])
    args = asr_server.exec_args_from_unit(unit, env)
    assert args == [str(tmp_path / "eng dir" / "crispasr"), "--server", "--host", "127.0.0.1",
                    "--port", "41234", "--backend", "qwen3", "-m", str(tmp_path / "m%.gguf"),
                    "-l", "en", "-t", "6", "-ng", "--hotwords", "Codemux,Hyprland"]


def test_render_unit_gpu_carries_vulkan_env(tmp_path: Path) -> None:
    unit = asr_server.render_unit(
        _spec(True, tmp_path, [("VOXTYPE_VULKAN_DEVICE", "nvidia"),
                               ("VK_LOADER_DRIVERS_SELECT", "nvidia*")]),
        tmp_path / "q.env")
    assert 'Environment="VK_LOADER_DRIVERS_SELECT=nvidia*"' in unit
    assert "-ng" not in asr_server.exec_args_from_unit(unit, "")


def test_server_spec_reads_gpu_dropin(qenv) -> None:
    st, md = qenv
    from voxtype_tui import gpu
    gpu.write_gpu_device(asr_server.gpu_dropin_path(), "nvidia")
    b = xm.crispasr_binary("vulkan")
    b.parent.mkdir(parents=True)
    b.write_text("x")
    b.chmod(0o755)
    asr_server.activate_qwen3(st, "0.6b-q4_k", models_dir=md, accel=GPU)
    spec = asr_server.server_spec(st.doc, st.sc, models_dir=md, accel=GPU)
    assert spec.gpu and dict(spec.env)["VK_LOADER_DRIVERS_SELECT"] == "nvidia*"
    assert spec.threads == 6 and spec.language == "en"


# --- save reconcile -----------------------------------------------------------


def test_save_writes_unit_env_and_enables_then_cleans_up(qenv, monkeypatch) -> None:
    st, md = qenv
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(asr_server, "_systemctl",
                        lambda *a, **k: (calls.append(a), (0, ""))[1])
    st.set_vocabulary(["Codemux", "Hyprland"])
    asr_server.set_active_model(st, ENGINE_QWEN3, "0.6b-q4_k", models_dir=md, accel=CPU512)
    monkeypatch.setattr(xm, "_ACCEL_CACHE", CPU512)
    monkeypatch.setattr(xm, "default_models_dir", lambda: md)
    fields = st.save()
    assert asr_server.RESTART_FIELD in fields and st.daemon_stale
    assert st.asr_problems == []
    assert ("daemon-reload",) in calls and ("enable", "voxtype-asr.service") in calls
    unit = asr_server.unit_path().read_text()
    assert f"--port {st.sc.qwen3_asr['port']}" in unit and " -ng " in unit
    assert asr_server.parse_env_value(asr_server.env_path().read_text()) == "Codemux,Hyprland"
    # marker persisted in metadata.json
    raw = json.loads(st.sidecar_path.read_text())
    assert raw["qwen3_asr"]["active"] is True
    assert sidecar.load(st.sidecar_path).qwen3_asr["model"] == "0.6b-q4_k"

    # vocabulary change -> env rewritten, unit untouched (no daemon-reload)
    calls.clear()
    st2 = state.AppState.load(st.config_path, st.sidecar_path)
    st2.add_vocab("Omarchy")
    fields = st2.save()
    assert asr_server.parse_env_value(asr_server.env_path().read_text()) == (
        "Codemux,Hyprland,Omarchy")
    assert ("daemon-reload",) not in calls and asr_server.RESTART_FIELD in fields

    # hotwords-only regen helper
    st2.sc.vocabulary.append(sidecar.VocabEntry("Waybar"))
    assert asr_server.regenerate_hotwords(st2.sc) is True
    assert asr_server.regenerate_hotwords(st2.sc) is False

    # switch back to whisper -> disable --now + files removed
    calls.clear()
    asr_server.set_active_model(st2, "whisper", "base.en", models_dir=md,
                                is_installed=_installed(md))
    st2.save()
    assert ("disable", "--now", "voxtype-asr.service") in calls
    assert not asr_server.unit_path().exists() and not asr_server.env_path().exists()


def test_save_without_qwen3_never_touches_systemd(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config.toml"
    shutil.copy(FIXTURES / "stock.toml", cfg)
    monkeypatch.setattr(asr_server, "_systemctl",
                        lambda *a, **k: pytest.fail(f"unexpected systemctl {a}"))
    st = state.AppState.load(cfg, tmp_path / "m.json")
    st.add_vocab("x")
    st.save()
    assert "qwen3_asr" not in json.loads((tmp_path / "m.json").read_text())


def test_save_reports_missing_engine(qenv, monkeypatch) -> None:
    st, md = qenv
    asr_server.activate_qwen3(st, "0.6b-q4_k", models_dir=md, accel=CPU512)
    shutil.rmtree(xm.build_dir("avx512"))
    monkeypatch.setattr(xm, "default_models_dir", lambda: md)
    st.save()
    assert st.asr_problems and "missing" in st.asr_problems[0]
    st2 = state.AppState.load(st.config_path, st.sidecar_path)
    assert any("dead endpoint" in w for w in st2.reconcile_warnings)


def test_status_reports_dead_server(qenv, monkeypatch) -> None:
    st, md = qenv
    asr_server.activate_qwen3(st, "0.6b-q4_k", models_dir=md, accel=CPU512,
                              port=asr_server.pick_free_port())
    monkeypatch.setattr(asr_server, "unit_status", lambda: {
        "available": True, "active_state": "failed", "result": "exit-code"})
    s = asr_server.status(st.doc, st.sc)
    assert s["engine_label"] == "Qwen3-ASR"
    assert s["qwen3"]["healthy"] is False
    assert s["qwen3"]["detail"] == "not running"
    assert "failed, exit-code" in s["qwen3"]["reason"]
    assert asr_server.status(st.doc, st.sc, probe=False)["qwen3"].get("healthy") is None


def test_status_detail_is_short_and_blames_port_only_when_taken(qenv, monkeypatch) -> None:
    import socket
    st, md = qenv
    port = asr_server.pick_free_port()
    asr_server.activate_qwen3(st, "0.6b-q4_k", models_dir=md, accel=CPU512, port=port)
    monkeypatch.setattr(asr_server, "unit_status", lambda: {"available": True, "active_state": "inactive"})
    monkeypatch.setattr(asr_server, "probe_health", lambda p: (False, "connection refused"))
    assert asr_server.status(st.doc, st.sc)["qwen3"]["detail"] == "not running"
    with socket.socket() as other:  # bound but not listening (like TIME-WAIT leftovers)
        other.bind(("127.0.0.1", port))
        assert asr_server.status(st.doc, st.sc)["qwen3"]["detail"] == "not running"
    with socket.socket() as other:  # another program really listens there
        other.bind(("127.0.0.1", port)); other.listen()
        assert asr_server.status(st.doc, st.sc)["qwen3"]["detail"] == "port taken by another program"
    monkeypatch.setattr(asr_server, "unit_status", lambda: {"available": True, "active_state": "deactivating"})
    assert asr_server.status(st.doc, st.sc)["qwen3"]["detail"] == "not running"
    monkeypatch.setattr(asr_server, "unit_status", lambda: {"available": True, "active_state": "activating"})
    assert asr_server.status(st.doc, st.sc)["qwen3"]["detail"] == "still starting"
    for v in ("not running", "still starting", "port taken by another program", "not responding"):
        assert len(v) < 40
