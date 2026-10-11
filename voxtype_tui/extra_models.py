"""Extra models voxtype itself doesn't ship: quantized whisper files and
Qwen3-ASR (served by a pinned CrispASR build, reached through voxtype's
whisper remote mode).

To the user these look like any other model: they sit in the same catalog,
download / delete / set-active the same way. Under the hood:

* **Quantized whisper** (``large-v3-turbo-q5_0`` …) — a ggml file in
  voxtype's models dir, named ``ggml-<name>.bin`` like the native ones so
  scan/size/delete keep working. voxtype rejects the catalog name but
  accepts a bare ``.bin`` filename resolved inside its models dir, so the
  config gets ``model = "ggml-<name>.bin"`` (:func:`config_model_value`)
  and every reader maps it back (:func:`catalog_model_name`). These files
  crash voxtype's CPU binaries, so they're only offered when voxtype runs
  its Vulkan binary (:func:`availability`).
* **Qwen3-ASR** — engine key ``qwen3-asr``. A GGUF in voxtype's models dir
  (``qwen3-asr-0.6b-<quant>.gguf``) plus a CrispASR build under
  ``$XDG_DATA_HOME/voxtype-tui/engines/``. Activation is in
  :mod:`voxtype_tui.asr_server`.

Everything here is importable without Textual (the Omarchy plugin bridge
uses it on its fast paths). The UI in ``models.py`` / ``settings.py``
calls into it. ``python -m voxtype_tui.extra_models download <engine>
<name>`` streams the same ``NN.N%`` progress frames ``voxtype setup
--download`` does, so the plugin can run it as a drop-in child process
(SIGTERM cancels and removes the ``.part`` file).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import downloader

# --- engine identity --------------------------------------------------------

ENGINE_QWEN3 = "qwen3-asr"
# Engines voxtype-tui adds on top of voxtype's own (UI engine keys; never
# written to `engine = …` in config.toml).
EXTRA_ENGINES: tuple[str, ...] = (ENGINE_QWEN3,)
# Display labels for engines whose key isn't a nice name on its own.
ENGINE_LABELS: dict[str, str] = {ENGINE_QWEN3: "Qwen3-ASR"}

REASON_NEEDS_GPU = "needs GPU acceleration"
REASON_NO_VOXTYPE = "voxtype is not installed"


def engine_label(engine: str) -> str:
    return ENGINE_LABELS.get(engine, engine)


# --- catalog ----------------------------------------------------------------

_HF = "https://huggingface.co"


@dataclass(frozen=True)
class ExtraModel:
    engine: str            # "whisper" or ENGINE_QWEN3
    name: str              # catalog name shown in the UI
    filename: str          # file in voxtype's models dir
    url: str
    sha256: str            # pinned (HF lfs.oid)
    size_bytes: int        # pinned
    requires_gpu: bool = False
    note: str = ""

    @property
    def size_mb(self) -> int:
        return round(self.size_bytes / (1024 * 1024))


# Pinned against the HF tree API (`lfs.oid` / `size`) of
# ggerganov/whisper.cpp @ 5359861c and cstr/qwen3-asr-0.6b-GGUF @ 58bd3202.
EXTRA_WHISPER_MODELS: tuple[ExtraModel, ...] = (
    ExtraModel(
        "whisper", "large-v3-turbo-q5_0", "ggml-large-v3-turbo-q5_0.bin",
        f"{_HF}/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/"
        "ggml-large-v3-turbo-q5_0.bin",
        "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        574041195, requires_gpu=True, note="quantized, GPU only",
    ),
    ExtraModel(
        "whisper", "large-v3-turbo-q8_0", "ggml-large-v3-turbo-q8_0.bin",
        f"{_HF}/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/"
        "ggml-large-v3-turbo-q8_0.bin",
        "317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1",
        874188075, requires_gpu=True, note="quantized, GPU only",
    ),
)

QWEN3_MODELS: tuple[ExtraModel, ...] = (
    ExtraModel(
        ENGINE_QWEN3, "0.6b-q8_0", "qwen3-asr-0.6b-q8_0.gguf",
        f"{_HF}/cstr/qwen3-asr-0.6b-GGUF/resolve/58bd3202f835f46b24b17142cb503b0860c737a5/"
        "qwen3-asr-0.6b-q8_0.gguf",
        "f547589d5ca582e093b2d3312ad9ff13b609b43d413f972c0e92b823dde70a00",
        1006809760, note="best accuracy",
    ),
    ExtraModel(
        ENGINE_QWEN3, "0.6b-q4_k", "qwen3-asr-0.6b-q4_k.gguf",
        f"{_HF}/cstr/qwen3-asr-0.6b-GGUF/resolve/58bd3202f835f46b24b17142cb503b0860c737a5/"
        "qwen3-asr-0.6b-q4_k.gguf",
        "f63771c02dfa486d9399d41ab6ab8cd2d8ca24e077cd32130ea1f67f4fd8dade",
        631026336, note="smaller, faster on CPU",
    ),
)

EXTRA_MODELS: tuple[ExtraModel, ...] = EXTRA_WHISPER_MODELS + QWEN3_MODELS

QWEN3_FILE_PREFIX = "qwen3-asr-"
QWEN3_FILE_SUFFIX = ".gguf"


@dataclass(frozen=True)
class CrispBuild:
    variant: str
    asset: str
    size_bytes: int
    sha256: str

    @property
    def url(self) -> str:
        return (
            f"https://github.com/CrispStrobe/CrispASR/releases/download/"
            f"v{CRISPASR_VERSION}/{self.asset}"
        )

    @property
    def dir_name(self) -> str:
        return f"crispasr-{CRISPASR_VERSION}-{self.variant}"


CRISPASR_VERSION = "0.8.42"
CRISPASR_BUILDS: dict[str, CrispBuild] = {
    b.variant: b for b in (
        CrispBuild("vulkan", "crispasr-linux-x86_64-vulkan.tar.gz", 70722373,
                   "355f6af387e4073aecbda95465da8f605c1621a74671ede7935f95e2aad42fe0"),
        CrispBuild("avx512", "crispasr-linux-x86_64-avx512.tar.gz", 42933420,
                   "04d1fc3ada2bcdf68aaca90000d79cf1db3f31b2e78dd3dcaa1c293bfdba01d8"),
        CrispBuild("generic", "crispasr-linux-x86_64.tar.gz", 42812132,
                   "1b942bb9bd85f9e732bb3c222dbc97a644d50a8f3a4378b1507d121fae39e91f"),
        CrispBuild("cpu-legacy", "crispasr-linux-x86_64-cpu-legacy.tar.gz", 42488451,
                   "83475071ef597db5014c412bcc3ae226320b660da01654a5762566de4e192992"),
    )
}


def get_extra(engine: str, name: str) -> ExtraModel | None:
    for m in EXTRA_MODELS:
        if m.engine == engine and m.name == name:
            return m
    return None


def is_extra_model(engine: str, name: str) -> bool:
    return get_extra(engine, name) is not None


def extra_names(engine: str) -> list[str]:
    return [m.name for m in EXTRA_MODELS if m.engine == engine]


# --- config value <-> catalog name -----------------------------------------


def config_model_value(engine: str, name: str) -> str:
    """Value to write into ``[<engine>] model`` for a catalog name.

    Extra whisper models become their bare filename (voxtype resolves
    ``*.bin`` inside its models dir but rejects unknown names); everything
    else is written verbatim.
    """
    m = get_extra(engine, name)
    if m is not None and engine == "whisper":
        return m.filename
    return name


def catalog_model_name(engine: str, value: str) -> str:
    """Inverse of :func:`config_model_value`: ``ggml-<name>.bin`` (bare
    filename, no directory) maps back to ``<name>``; anything else is
    returned unchanged. Use wherever the active model is shown/compared."""
    if engine != "whisper" or not value:
        return value
    if "/" in value or not (value.startswith("ggml-") and value.endswith(".bin")):
        return value
    return value[len("ggml-"):-len(".bin")]


# --- paths ------------------------------------------------------------------


def _data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    return Path(raw) if raw else Path.home() / ".local" / "share"


def default_models_dir() -> Path:
    """voxtype's models dir, resolved like voxtype does (XDG data dir).
    ``VOXTYPE_MODELS_DIR`` (the plugin bridge's test override) wins."""
    override = os.environ.get("VOXTYPE_MODELS_DIR")
    if override:
        return Path(override).expanduser()
    return _data_home() / "voxtype" / "models"


def engines_dir() -> Path:
    return _data_home() / "voxtype-tui" / "engines"


def model_path(m: ExtraModel, models_dir: Path | None = None) -> Path:
    return (models_dir or default_models_dir()) / m.filename


def build_dir(variant: str) -> Path:
    return engines_dir() / CRISPASR_BUILDS[variant].dir_name


def crispasr_binary(variant: str) -> Path:
    return build_dir(variant) / "crispasr"


def build_installed(variant: str) -> bool:
    b = crispasr_binary(variant)
    return b.is_file() and os.access(b, os.X_OK)


def is_downloaded(engine: str, name: str, models_dir: Path | None = None) -> bool:
    m = get_extra(engine, name)
    if m is None:
        return False
    p = model_path(m, models_dir)
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def scan_qwen3(models_dir: Path | None = None) -> dict[str, int]:
    """{catalog_name: size_bytes} for Qwen3 GGUFs on disk (``.part`` files
    are ignored by the glob)."""
    base = models_dir or default_models_dir()
    out: dict[str, int] = {}
    if not base.is_dir():
        return out
    for f in base.glob(f"{QWEN3_FILE_PREFIX}*{QWEN3_FILE_SUFFIX}"):
        name = f.name[len(QWEN3_FILE_PREFIX):-len(QWEN3_FILE_SUFFIX)]
        try:
            out[name] = f.stat().st_size
        except OSError:
            continue
    return out


# --- hardware / voxtype binary detection -----------------------------------


@dataclass(frozen=True)
class Accel:
    """What voxtype runs on and what the CPU supports."""

    voxtype_installed: bool
    voxtype_variant: str | None    # e.g. "whisper-vulkan", "whisper-avx512"
    voxtype_gpu: bool              # True only for the Vulkan whisper binary
    cpu: str                       # "avx512" | "avx2" | "legacy"
    physical_cores: int


_ACCEL_CACHE: Accel | None = None


def _cpu_flags() -> set[str]:
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return set()
    for line in text.splitlines():
        if line.startswith("flags"):
            return set(line.split(":", 1)[1].split())
    return set()


def physical_cores() -> int:
    """Count distinct (physical id, core id) pairs; falls back to half the
    logical CPUs (SMT) or 1."""
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        text = ""
    cores: set[tuple[str, str]] = set()
    phys = core = None
    for line in text.splitlines() + [""]:
        if not line.strip():
            if core is not None:
                cores.add((phys or "0", core))
            phys = core = None
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        if key == "physical id":
            phys = val.strip()
        elif key == "core id":
            core = val.strip()
    if cores:
        return len(cores)
    n = os.cpu_count() or 2
    return max(1, n // 2)


def _voxtype_variant() -> tuple[bool, str | None]:
    if shutil.which("voxtype") is None:
        return False, None
    try:
        r = subprocess.run(
            ["voxtype", "info", "variants", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(r.stdout) if r.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        data = {}
    variant = data.get("running_variant") or data.get("active_variant")
    if not variant:
        binary = data.get("binary_path") or shutil.which("voxtype") or ""
        try:
            binary = os.path.realpath(binary)
        except OSError:
            pass
        base = os.path.basename(binary)
        if "vulkan" in base:
            variant = "whisper-vulkan"
        elif base.startswith("voxtype-"):
            variant = "whisper-" + base[len("voxtype-"):]
    return True, variant


def detect_accel(refresh: bool = False) -> Accel:
    """Probe once per process (``voxtype info variants --json`` + /proc)."""
    global _ACCEL_CACHE
    if _ACCEL_CACHE is not None and not refresh:
        return _ACCEL_CACHE
    installed, variant = _voxtype_variant()
    flags = _cpu_flags()
    if "avx512f" in flags:
        cpu = "avx512"
    elif "avx2" in flags:
        cpu = "avx2"
    else:
        cpu = "legacy"
    _ACCEL_CACHE = Accel(
        voxtype_installed=installed,
        voxtype_variant=variant,
        voxtype_gpu=bool(variant and "vulkan" in variant),
        cpu=cpu,
        physical_cores=physical_cores(),
    )
    return _ACCEL_CACHE


def crispasr_variant(accel: Accel | None = None) -> str:
    """vulkan when voxtype itself runs on GPU, else avx512 / generic (avx2)
    / cpu-legacy by CPU flags."""
    a = accel or detect_accel()
    if a.voxtype_gpu:
        return "vulkan"
    return {"avx512": "avx512", "avx2": "generic"}.get(a.cpu, "cpu-legacy")


def availability(engine: str, name: str, accel: Accel | None = None) -> tuple[bool, str | None]:
    """(selectable/downloadable, disabled_reason). Native models are always
    (True, None) here; the existing compiled-engine checks still apply."""
    m = get_extra(engine, name)
    if m is None:
        return True, None
    a = accel or detect_accel()
    if not a.voxtype_installed:
        return False, REASON_NO_VOXTYPE
    if m.requires_gpu and not a.voxtype_gpu:
        return False, REASON_NEEDS_GPU
    return True, None


def quantized_whisper_warning(doc: Any, accel: Accel | None = None) -> str | None:
    """Warning text when the active whisper model is a GPU-only quantized
    file but voxtype is not running its Vulkan binary (the daemon would
    crash or crawl)."""
    try:
        engine = str(doc.get("engine") or "whisper").lower()
        whisper = doc.get("whisper") or {}
        mode = str(whisper.get("mode") or whisper.get("backend") or "local")
        value = str(whisper.get("model") or "")
    except AttributeError:
        return None
    if engine != "whisper" or mode == "remote":
        return None
    m = get_extra("whisper", catalog_model_name("whisper", value))
    if m is None or not m.requires_gpu:
        return None
    a = accel or detect_accel()
    if a.voxtype_gpu:
        return None
    return (
        f"Active model {m.name} needs GPU acceleration, but voxtype is not "
        f"using its Vulkan build ({a.voxtype_variant or 'unknown'}). Enable GPU "
        "acceleration or pick another model, or dictation will fail."
    )


# --- listing ----------------------------------------------------------------


def list_extra_models(
    engine: str | None = None,
    *,
    models_dir: Path | None = None,
    accel: Accel | None = None,
) -> list[dict[str, Any]]:
    """Rows for the extra catalog (optionally one engine), each:
    ``{engine, engine_label, name, size_mb, downloaded, on_disk_bytes,
    available, disabled_reason, path, note}``."""
    a = accel or detect_accel()
    rows: list[dict[str, Any]] = []
    for m in EXTRA_MODELS:
        if engine is not None and m.engine != engine:
            continue
        p = model_path(m, models_dir)
        try:
            size = p.stat().st_size if p.is_file() else None
        except OSError:
            size = None
        ok, reason = availability(m.engine, m.name, a)
        if ok and m.engine == ENGINE_QWEN3 and size and not build_installed(crispasr_variant(a)):
            reason = "engine not downloaded — press Download"
        rows.append({
            "engine": m.engine,
            "engine_label": engine_label(m.engine),
            "name": m.name,
            "size_mb": m.size_mb,
            "downloaded": bool(size),
            "on_disk_bytes": size,
            "available": ok,
            "disabled_reason": reason,
            "path": str(p),
            "note": m.note,
        })
    return rows


# --- download / delete ------------------------------------------------------


class RefusedError(RuntimeError):
    """An explicit action was refused; message says why (show as warning)."""


ProgressCb = Callable[[float, str], None]   # (percent 0..100, label)


def download(
    engine: str,
    name: str,
    *,
    models_dir: Path | None = None,
    progress: ProgressCb | None = None,
    cancel: threading.Event | None = None,
    accel: Accel | None = None,
) -> Path:
    """Download an extra model (and, for Qwen3, the matching CrispASR
    build) with sha256 verification. Already-present files of the pinned
    size are skipped. Returns the model path.

    Raises :class:`RefusedError` (unknown / unavailable model),
    :class:`downloader.DownloadCancelled`, :class:`downloader.ChecksumError`
    or :class:`downloader.DownloadError`. A failed or cancelled run leaves
    no ``.part`` files behind.
    """
    m = get_extra(engine, name)
    if m is None:
        raise RefusedError(f"'{name}' is not an extra {engine} model")
    a = accel or detect_accel()
    ok, reason = availability(engine, name, a)
    if not ok:
        raise RefusedError(f"{name}: {reason}")
    jobs: list[tuple[str, int, Callable[[downloader.ProgressFn], None]]] = []
    if engine == ENGINE_QWEN3:
        variant = crispasr_variant(a)
        if not build_installed(variant):
            build = CRISPASR_BUILDS[variant]

            def fetch_build(cb: downloader.ProgressFn, build: CrispBuild = build) -> None:
                tar = engines_dir() / ".downloads" / build.asset
                downloader.download_verified(
                    build.url, tar, build.sha256, size=build.size_bytes,
                    progress=cb, cancel=cancel,
                )
                try:
                    downloader.extract_tarball(tar, build_dir(build.variant), expect_file="crispasr")
                finally:
                    tar.unlink(missing_ok=True)

            jobs.append((f"CrispASR {CRISPASR_VERSION} ({variant})", build.size_bytes, fetch_build))
    dest = model_path(m, models_dir)
    have = dest.is_file() and dest.stat().st_size == m.size_bytes
    if not have:
        def fetch_model(cb: downloader.ProgressFn) -> None:
            downloader.download_verified(
                m.url, dest, m.sha256, size=m.size_bytes, progress=cb, cancel=cancel,
            )
        jobs.append((m.filename, m.size_bytes, fetch_model))

    total = sum(size for _, size, _ in jobs) or 1
    base = 0
    for label, size, run in jobs:
        def cb(done: int, _total: int | None, base: int = base, label: str = label) -> None:
            if progress is not None:
                progress(min(100.0, (base + done) * 100.0 / total), label)
        run(cb)
        base += size
    if progress is not None:
        progress(100.0, m.filename)
    return dest


def remove_engine_builds() -> list[Path]:
    """Delete every CrispASR build (called when no Qwen3 model is left)."""
    removed: list[Path] = []
    root = engines_dir()
    if not root.is_dir():
        return removed
    for p in root.iterdir():
        if p.is_dir() and (p.name.startswith("crispasr-") or p.name == ".downloads"):
            shutil.rmtree(p, ignore_errors=True)
            removed.append(p)
    return removed


def cleanup_partial(engine: str, name: str, models_dir: Path | None = None) -> bool:
    """Remove a leftover ``.part`` for this model (e.g. after the download
    child was SIGKILLed). Returns True when something was removed."""
    m = get_extra(engine, name)
    if m is None:
        return False
    return downloader.remove_stale_part(model_path(m, models_dir))


def delete(
    engine: str,
    name: str,
    *,
    doc: Any = None,
    sc: Any = None,
    models_dir: Path | None = None,
) -> int:
    """Delete an extra model file; returns bytes freed. Refuses while the
    model is active (pass the loaded ``doc``/``sc``). Deleting the last
    Qwen3 model also removes the CrispASR build(s)."""
    m = get_extra(engine, name)
    if m is None:
        raise RefusedError(f"'{name}' is not an extra {engine} model")
    if doc is not None:
        from . import asr_server
        act_engine, act_model = asr_server.active_engine_model(doc, sc)
        if act_engine == engine and act_model == name:
            raise RefusedError(
                f"'{name}' is the active {engine_label(engine)} model — "
                "set a different model active first"
            )
    p = model_path(m, models_dir)
    downloader.remove_stale_part(p)
    if not p.exists():
        raise RefusedError(f"'{name}' is not downloaded — nothing to delete")
    freed = p.stat().st_size
    p.unlink()
    if engine == ENGINE_QWEN3 and not scan_qwen3(models_dir):
        remove_engine_builds()
    return freed


def download_argv(engine: str, name: str) -> list[str]:
    """argv that downloads (engine, name) printing ``NN.N%`` progress frames.

    Native models: ``voxtype setup --download --model <name>`` (which also
    makes the model active in config.toml). Extra models: this module's
    sha256-verified downloader, ``<python> -m voxtype_tui.extra_models
    download <engine> <name>`` — same output protocol, exit 0 on success,
    SIGTERM cancels and removes the ``.part`` file, and it never touches
    config.toml. The child honours ``VOXTYPE_MODELS_DIR``.
    """
    if is_extra_model(engine, name):
        return [sys.executable, "-m", "voxtype_tui.extra_models", "download", engine, name]
    return ["voxtype", "setup", "--download", "--model", name]


# --- CLI: progress-streaming child for the plugin ---------------------------


def _cli_download(engine: str, name: str) -> int:
    cancel = threading.Event()

    def on_signal(_signum, _frame) -> None:
        cancel.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    out = sys.stdout
    last = [-1.0, ""]

    def progress(pct: float, label: str) -> None:
        if label != last[1]:
            if last[1]:
                out.write("\n")
            out.write(f"Downloading {label}\n")
            last[1] = label
        if pct - last[0] >= 0.5 or pct >= 100.0:
            last[0] = pct
            out.write(f"\r{label}: {pct:5.1f}%")
            out.flush()

    try:
        path = download(engine, name, progress=progress, cancel=cancel)
    except downloader.DownloadCancelled:
        out.write("\nCancelled; partial file removed\n")
        out.flush()
        return 130
    except (RefusedError, downloader.DownloadError) as e:
        out.write(f"\nError: {e}\n")
        out.flush()
        return 1
    out.write(f"\nVerified sha256, saved {path}\n")
    out.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "download":
        return _cli_download(args[1], args[2])
    if len(args) >= 1 and args[0] == "list":
        print(json.dumps(list_extra_models(args[1] if len(args) > 1 else None), indent=1))
        return 0
    sys.stderr.write(
        "usage: python -m voxtype_tui.extra_models download <engine> <name>\n"
        "       python -m voxtype_tui.extra_models list [engine]\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
