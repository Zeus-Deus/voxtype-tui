"""Qwen3-ASR as a managed voxtype engine.

voxtype can't run Qwen3-ASR itself, but its whisper *remote* mode posts
audio to any OpenAI-style ``/v1/audio/transcriptions`` server. So "Qwen3-ASR
active" means:

* a pinned CrispASR build serves the Qwen3 GGUF on ``127.0.0.1:<port>``
  from a systemd user unit, ``voxtype-asr.service``, that follows
  ``voxtype.service`` (``PartOf=`` + ``Before=`` + ``WantedBy=``), so
  every ``systemctl --user start/stop/restart voxtype`` drives the server
  too and new hotwords load on the same restart voxtype already needs;
* voxtype's config says ``engine = "whisper"`` + ``[whisper] mode =
  "remote"``, ``remote_endpoint = "http://127.0.0.1:<port>"``,
  ``remote_model = "qwen3-asr"``, ``remote_timeout_secs = 60``;
* the sidecar (``metadata.json``) carries a ``qwen3_asr`` block: the
  active flag, model, port, CrispASR variant and the whisper settings that
  were in place before, so switching away restores them.

"Qwen3-ASR is active" is the sidecar marker AND a matching endpoint
(:func:`is_qwen3_active`) — never the endpoint alone, so a user-configured
remote server still shows as remote whisper.

Vocabulary: voxtype's remote client only sends ``prompt``, which Qwen3
ignores, so the vocabulary goes to the server as ``--hotwords`` read from
an EnvironmentFile that :func:`apply_after_save` rewrites on every save.

Lifecycle of the files is reconciled in one place, :func:`apply_after_save`,
which ``AppState.save`` calls after config.toml + metadata.json committed.
Mutations (:func:`set_active_model`, :func:`activate_qwen3`,
:func:`deactivate_qwen3`) only touch the in-memory ``AppState``; the
caller saves (and restarts voxtype) as for any other setting.

Stdlib only (plus sibling non-UI modules); no Textual import.
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import shutil
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import extra_models as xm
from .extra_models import ENGINE_QWEN3, RefusedError

UNIT_NAME = "voxtype-asr.service"
VOXTYPE_UNIT = "voxtype.service"
REMOTE_MODEL = "qwen3-asr"
REMOTE_TIMEOUT_SECS = 60
HOTWORDS_VAR = "VOXTYPE_ASR_HOTWORDS"
SIDECAR_KEY = "qwen3_asr"
UNIT_MARKER = "# Managed by voxtype-tui"
# Pseudo restart-sensitive field reported by AppState.save when only the
# server side (unit / hotwords) changed.
RESTART_FIELD = "qwen3_asr.server"

# [whisper] keys we overwrite on activation and restore on deactivation.
_WHISPER_KEYS = ("mode", "backend", "remote_endpoint", "remote_model", "remote_timeout_secs")


# --- paths (resolved at call time so HOME/XDG overrides work) ---------------


def _config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    return Path(raw) if raw else Path.home() / ".config"


def unit_path() -> Path:
    return _config_home() / "systemd" / "user" / UNIT_NAME


def env_path() -> Path:
    return _config_home() / "voxtype-tui" / "qwen3-asr.env"


def gpu_dropin_path() -> Path:
    return _config_home() / "systemd" / "user" / "voxtype.service.d" / "gpu.conf"


# --- sidecar marker ---------------------------------------------------------


def qwen3_state(sc: Any) -> dict[str, Any]:
    """The ``qwen3_asr`` sidecar block as a dict ({} when absent). Accepts a
    ``sidecar.Sidecar`` or the raw metadata.json dict (bridge fast path)."""
    if sc is None:
        return {}
    raw = sc.get(SIDECAR_KEY) if isinstance(sc, dict) else getattr(sc, SIDECAR_KEY, None)
    return raw if isinstance(raw, dict) else {}


def _set_qwen3_state(sc: Any, block: dict[str, Any] | None) -> None:
    if isinstance(sc, dict):
        if block is None:
            sc.pop(SIDECAR_KEY, None)
        else:
            sc[SIDECAR_KEY] = block
    else:
        setattr(sc, SIDECAR_KEY, block)


def endpoint_for(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _get(node: Any, *keys: str) -> Any:
    for k in keys:
        if node is None or not hasattr(node, "get"):
            return None
        node = node.get(k)
    return node


def is_qwen3_active(doc: Any, sc: Any) -> bool:
    """Sidecar marker says active AND config points at our endpoint."""
    st = qwen3_state(sc)
    if not st.get("active"):
        return False
    port = st.get("port")
    if not isinstance(port, int):
        return False
    engine = str(_get(doc, "engine") or "whisper").lower()
    mode = str(_get(doc, "whisper", "mode") or "")
    endpoint = str(_get(doc, "whisper", "remote_endpoint") or "").rstrip("/")
    return engine == "whisper" and mode == "remote" and endpoint == endpoint_for(port)


def active_engine_model(doc: Any, sc: Any) -> tuple[str, str]:
    """(engine, catalog model name) as the UI should show them.

    ``("qwen3-asr", "0.6b-q4_k")`` when Qwen3 is active; whisper models
    written as ``ggml-<name>.bin`` map back to ``<name>``. The model is ""
    when unset."""
    if is_qwen3_active(doc, sc):
        return ENGINE_QWEN3, str(qwen3_state(sc).get("model") or "")
    engine = str(_get(doc, "engine") or "whisper").strip().lower()
    value = _get(doc, engine, "model")
    value = str(value) if value else ""
    return engine, xm.catalog_model_name(engine, value)


def ui_engine(doc: Any, sc: Any) -> str:
    return active_engine_model(doc, sc)[0]


# --- port -------------------------------------------------------------------


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def port_has_listener(port: int, timeout: float = 0.3) -> bool:
    """True when some program accepts TCP connections on 127.0.0.1:port.
    Unlike a bind test, sockets left in TIME-WAIT by a server that just
    exited don't count."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --- variant / build resolution ---------------------------------------------


def usable_variant(accel: xm.Accel | None = None) -> str | None:
    """Preferred CrispASR variant if installed, else any installed variant
    this CPU can run (CPU builds work regardless of voxtype's binary)."""
    a = accel or xm.detect_accel()
    preferred = xm.crispasr_variant(a)
    if xm.build_installed(preferred):
        return preferred
    fallbacks = {"avx512": ["avx512", "generic", "cpu-legacy"],
                 "avx2": ["generic", "cpu-legacy"]}.get(a.cpu, ["cpu-legacy"])
    for v in fallbacks:
        if xm.build_installed(v):
            return v
    return None


# --- mutations (in-memory; caller saves) -------------------------------------


def _ensure_table(doc: Any, key: str) -> Any:
    if key not in doc:
        import tomlkit
        doc[key] = tomlkit.table()
    return doc[key]


def _snapshot_whisper(doc: Any) -> dict[str, Any]:
    whisper = _get(doc, "whisper") or {}
    out: dict[str, Any] = {"engine": str(_get(doc, "engine") or "whisper")}
    for k in _WHISPER_KEYS:
        v = whisper.get(k) if hasattr(whisper, "get") else None
        if v is not None:
            v = int(v) if isinstance(v, int) and not isinstance(v, bool) else str(v)
        out[k] = v
    return out


def _set(st: Any, path: str, value: Any) -> None:
    if value is None:
        st.unset_setting(path)
    else:
        st.set_setting(path, value)


def activate_qwen3(
    st: Any,
    name: str,
    *,
    port: int | None = None,
    models_dir: Path | None = None,
    accel: xm.Accel | None = None,
) -> dict[str, Any]:
    """Point ``st`` (an ``AppState``) at Qwen3-ASR model ``name``.

    Refuses (:class:`RefusedError`) when the GGUF or a usable CrispASR build
    isn't downloaded. Remembers the current whisper settings unless Qwen3
    is already active (model switch keeps the original ones). Returns
    ``{"engine", "model", "port", "endpoint", "variant"}``."""
    m = xm.get_extra(ENGINE_QWEN3, name)
    if m is None:
        raise RefusedError(f"unknown Qwen3-ASR model {name!r}")
    if not xm.is_downloaded(ENGINE_QWEN3, name, models_dir):
        raise RefusedError(f"'{name}' isn't downloaded yet — download it first")
    a = accel or xm.detect_accel()
    if not a.voxtype_installed:
        raise RefusedError(f"Qwen3-ASR: {xm.REASON_NO_VOXTYPE}")
    variant = usable_variant(a)
    if variant is None:
        raise RefusedError(
            "the Qwen3-ASR engine (CrispASR) isn't downloaded — press Download on the model"
        )
    already = is_qwen3_active(st.doc, st.sc)
    prev_block = qwen3_state(st.sc)
    if port is None:
        old = prev_block.get("port")
        if already and isinstance(old, int):
            port = old  # our own server may hold it; keep the endpoint stable
        elif isinstance(old, int) and port_is_free(old):
            port = old
        else:
            port = pick_free_port()
    previous = prev_block.get("previous_whisper") if already else _snapshot_whisper(st.doc)
    _ensure_table(st.doc, "whisper")
    st.set_setting("engine", "whisper")
    st.set_setting("whisper.mode", "remote")
    if _get(st.doc, "whisper", "backend") is not None:
        st.unset_setting("whisper.backend")
    st.set_setting("whisper.remote_endpoint", endpoint_for(port))
    st.set_setting("whisper.remote_model", REMOTE_MODEL)
    st.set_setting("whisper.remote_timeout_secs", REMOTE_TIMEOUT_SECS)
    block = {
        "active": True,
        "model": name,
        "port": port,
        "variant": variant,
        "previous_whisper": previous,
    }
    if block != prev_block:
        _set_qwen3_state(st.sc, block)
        st.sidecar_dirty = True
    return {"engine": ENGINE_QWEN3, "model": name, "port": port,
            "endpoint": endpoint_for(port), "variant": variant}


def deactivate_qwen3(st: Any, *, restore_engine: bool = True) -> bool:
    """Undo :func:`activate_qwen3`: restore the remembered whisper settings
    (and the previous engine when ``restore_engine``). Returns True when
    Qwen3 was active. The unit is disabled/stopped by the next save
    (:func:`apply_after_save`)."""
    block = qwen3_state(st.sc)
    was_active = is_qwen3_active(st.doc, st.sc)
    if was_active:
        prev = block.get("previous_whisper") or {}
        for k in _WHISPER_KEYS:
            _set(st, f"whisper.{k}", prev.get(k))
        if restore_engine:
            st.set_setting("engine", str(prev.get("engine") or "whisper"))
    if block.get("active"):
        new_block = dict(block, active=False)
        new_block.pop("previous_whisper", None)
        _set_qwen3_state(st.sc, new_block)
        st.sidecar_dirty = True
    return was_active


def set_active_model(
    st: Any,
    engine: str,
    name: str,
    *,
    models_dir: Path | None = None,
    accel: xm.Accel | None = None,
    is_installed: Any = None,
    port: int | None = None,
) -> dict[str, Any]:
    """One entry point for "make (engine, name) the active model".

    * ``engine == "qwen3-asr"`` → :func:`activate_qwen3` (engine switch
      included, previous whisper settings remembered);
    * otherwise Qwen3 is deactivated first (whisper settings restored),
      then ``engine`` and ``[<engine>] model`` are set — extra whisper
      models as their bare ``ggml-<name>.bin`` filename.

    Refuses (:class:`RefusedError`) a model that isn't downloaded or isn't
    available on this machine (quantized whisper without the Vulkan
    binary). ``is_installed(engine, name)`` checks native models; it
    defaults to :func:`native_model_installed`. Does not save.
    """
    if engine == ENGINE_QWEN3:
        return activate_qwen3(st, name, port=port, models_dir=models_dir, accel=accel)
    if xm.is_extra_model(engine, name):
        ok, reason = xm.availability(engine, name, accel)
        if not ok:
            raise RefusedError(f"{name}: {reason}")
        installed = xm.is_downloaded(engine, name, models_dir)
    else:
        check = is_installed or (lambda e, n: native_model_installed(e, n, models_dir))
        installed = check(engine, name)
    if not installed:
        raise RefusedError(f"'{name}' isn't downloaded yet — download it first")
    deactivate_qwen3(st, restore_engine=False)
    st.set_setting("engine", engine)
    st.set_setting(f"{engine}.model", xm.config_model_value(engine, name))
    return {"engine": engine, "model": name}


# Mirrors models.model_file_path for native engines (kept here so this
# module stays Textual-free); pinned by tests/test_extra_models.py.
NATIVE_PATH_TEMPLATES: dict[str, str] = {
    "whisper": "ggml-{name}.bin",
    "moonshine": "moonshine-{name}",
    "sensevoice": "sensevoice-{name}",
    "paraformer": "paraformer-{name}",
    "dolphin": "dolphin-{name}",
    "omnilingual": "omnilingual-{name}",
    "parakeet": "{name}",
}


def native_model_installed(engine: str, name: str, models_dir: Path | None = None) -> bool:
    base = models_dir or xm.default_models_dir()
    if engine == "whisper":
        name = xm.catalog_model_name("whisper", name)
    p = base / NATIVE_PATH_TEMPLATES.get(engine, "{name}.bin").format(name=name)
    try:
        if p.is_file():
            return p.stat().st_size > 0
        if p.is_dir():
            return any(f.is_file() for f in p.rglob("*"))
    except OSError:
        return False
    return False


# --- rendering --------------------------------------------------------------


def hotwords(phrases: list[str]) -> str:
    """Comma-joined hotword list for ``--hotwords``. Commas inside a phrase
    would split it, so they become spaces; control characters are dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for p in phrases:
        clean = "".join(ch for ch in str(p) if ch.isprintable()).replace(",", " ")
        clean = " ".join(clean.split())
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return ",".join(out)


def render_env(phrases: list[str]) -> str:
    """EnvironmentFile body. systemd reads double-quoted values with
    backslash escapes; ``$`` is escaped too so nothing is expanded."""
    value = hotwords(phrases)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return (
        f"{UNIT_MARKER}; rewritten on every save from the vocabulary.\n"
        f'{HOTWORDS_VAR}="{escaped}"\n'
    )


def parse_env_value(text: str) -> str:
    """Inverse of :func:`render_env` (used by tests and the host e2e)."""
    for line in text.splitlines():
        if line.startswith(HOTWORDS_VAR + "="):
            raw = line.split("=", 1)[1].strip()
            if raw.startswith('"') and raw.endswith('"'):
                raw = raw[1:-1]
            out, i = [], 0
            while i < len(raw):
                if raw[i] == "\\" and i + 1 < len(raw):
                    out.append(raw[i + 1])
                    i += 2
                else:
                    out.append(raw[i])
                    i += 1
            return "".join(out)
    return ""


def _language(doc: Any) -> str:
    lang = _get(doc, "whisper", "language")
    if isinstance(lang, (list, tuple)) or (hasattr(lang, "__iter__") and not isinstance(lang, str)):
        try:
            lang = list(lang)[0]
        except (IndexError, TypeError):
            lang = None
    lang = str(lang).strip() if lang else ""
    return lang or "en"


@dataclass(frozen=True)
class ServerSpec:
    binary: Path
    model: Path
    port: int
    threads: int
    language: str
    gpu: bool
    env: tuple[tuple[str, str], ...]   # extra Environment= (Vulkan device pinning)

    def exec_args(self, hotword_arg: str) -> list[str]:
        args = [
            str(self.binary), "--server", "--host", "127.0.0.1", "--port", str(self.port),
            "--backend", "qwen3", "-m", str(self.model), "-l", self.language,
            "-t", str(self.threads),
        ]
        if not self.gpu:
            args.append("-ng")
        args += ["--hotwords", hotword_arg]
        return args


def server_spec(
    doc: Any,
    sc: Any,
    *,
    models_dir: Path | None = None,
    accel: xm.Accel | None = None,
) -> ServerSpec | None:
    block = qwen3_state(sc)
    m = xm.get_extra(ENGINE_QWEN3, str(block.get("model") or ""))
    port = block.get("port")
    if m is None or not isinstance(port, int):
        return None
    a = accel or xm.detect_accel()
    variant = str(block.get("variant") or "") or usable_variant(a)
    if not variant or variant not in xm.CRISPASR_BUILDS:
        return None
    gpu = variant == "vulkan"
    env: list[tuple[str, str]] = []
    if gpu:
        from . import gpu as gpu_mod
        vendor = gpu_mod.read_gpu_device(gpu_dropin_path())
        if vendor:
            env.append(("VOXTYPE_VULKAN_DEVICE", vendor))
            pattern = gpu_mod.VENDOR_LOADER_PATTERNS.get(vendor)
            if pattern:
                env.append((gpu_mod.LOADER_ENV_VAR, pattern))
    return ServerSpec(
        binary=xm.crispasr_binary(variant),
        model=xm.model_path(m, models_dir),
        port=port,
        threads=a.physical_cores,
        language=_language(doc),
        gpu=gpu,
        env=tuple(env),
    )


def _unit_quote(arg: str) -> str:
    """Quote one ExecStart argument for systemd: double quotes, escaped
    backslash/quote, ``%`` doubled (specifiers)."""
    if arg == "${%s}" % HOTWORDS_VAR:
        return arg
    a = arg.replace("%", "%%")
    if a and all(ch.isalnum() or ch in "-_./:=+,@" for ch in a):
        return a
    return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_unit(spec: ServerSpec, env_file: Path) -> str:
    exec_line = " ".join(_unit_quote(a) for a in spec.exec_args("${%s}" % HOTWORDS_VAR))
    env_lines = "".join(f'Environment="{k}={v}"\n' for k, v in spec.env)
    return (
        f"{UNIT_MARKER}; regenerated on every save, do not edit.\n"
        "[Unit]\n"
        "Description=Qwen3-ASR server for voxtype (CrispASR)\n"
        f"PartOf={VOXTYPE_UNIT}\n"
        f"Before={VOXTYPE_UNIT}\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"EnvironmentFile={_unit_quote(str(env_file))}\n"
        f"{env_lines}"
        f"WorkingDirectory={_unit_quote(str(spec.binary.parent))}\n"
        f"ExecStart={exec_line}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        f"WantedBy={VOXTYPE_UNIT}\n"
    )


def exec_args_from_unit(unit_text: str, env_text: str) -> list[str]:
    """Parse ExecStart from a rendered unit and substitute the hotwords
    from the env file — what systemd would exec (used by the host e2e and
    tests; handles only what :func:`render_unit` emits)."""
    for line in unit_text.splitlines():
        if line.startswith("ExecStart="):
            args = shlex.split(line[len("ExecStart="):])
            value = parse_env_value(env_text)
            return [value if a == "${%s}" % HOTWORDS_VAR else a.replace("%%", "%") for a in args]
    raise ValueError("no ExecStart line")


# --- systemctl --------------------------------------------------------------


def _systemctl(*args: str, timeout: float = 15.0) -> tuple[int, str]:
    """``systemctl --user <args>``; (code, message). ``VOXTYPE_TUI_SYSTEMCTL=0``
    turns it into a no-op (isolated host runs that must not reach the real
    user manager)."""
    if os.environ.get("VOXTYPE_TUI_SYSTEMCTL") == "0":
        return 0, "systemctl skipped (VOXTYPE_TUI_SYSTEMCTL=0)"
    if shutil.which("systemctl") is None:
        return 127, "systemctl not found"
    try:
        r = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"systemctl {' '.join(args)} timed out"
    except OSError as e:
        return 126, str(e)
    return r.returncode, (r.stderr or r.stdout).strip()


def _write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text() == text:
            return False
    except OSError:
        pass
    from .gpu import _atomic_write
    _atomic_write(path, text)
    return True


def _owned_unit_exists() -> bool:
    try:
        return UNIT_MARKER in unit_path().read_text()
    except OSError:
        return False


@dataclass
class ApplyResult:
    """Outcome of :func:`apply_after_save`."""

    # True when the unit or the hotwords file changed, or the unit was
    # removed — voxtype needs a restart for it to take effect (the server
    # follows voxtype's restart through PartOf=).
    changed: bool = False
    problems: list[str] = field(default_factory=list)


def apply_after_save(
    doc: Any,
    sc: Any,
    *,
    models_dir: Path | None = None,
    accel: xm.Accel | None = None,
) -> ApplyResult:
    """Reconcile the unit + env file with the saved state. Called by
    ``AppState.save`` after config.toml and metadata.json committed.

    * Qwen3 active → (re)write the hotwords env file and the unit,
      ``daemon-reload`` when the unit changed, ``enable`` it (adds it to
      ``voxtype.service.wants``). The server starts/restarts with the next
      voxtype start/restart.
    * not active but our unit exists → ``disable --now``, remove unit and
      env file, ``daemon-reload``.
    * no marker and no unit → nothing (no filesystem or systemctl access).

    Never raises; problems come back in :attr:`ApplyResult.problems`.
    """
    res = ApplyResult()
    problems = res.problems
    block = qwen3_state(sc)
    try:
        if is_qwen3_active(doc, sc):
            spec = server_spec(doc, sc, models_dir=models_dir, accel=accel)
            if spec is None or not spec.binary.is_file() or not spec.model.is_file():
                problems.append("Qwen3-ASR is active but its engine or model file is missing — "
                                "download it again or switch models")
                return res
            env_changed = _write_if_changed(env_path(), render_env(vocab_phrases(sc)))
            changed = _write_if_changed(unit_path(), render_unit(spec, env_path()))
            res.changed = env_changed or changed
            if changed:
                code, msg = _systemctl("daemon-reload")
                if code != 0:
                    problems.append(f"systemctl daemon-reload failed: {msg}")
            code, msg = _systemctl("enable", UNIT_NAME)
            if code != 0:
                problems.append(f"could not enable {UNIT_NAME}: {msg}")
        elif block or _owned_unit_exists():
            if _owned_unit_exists():
                res.changed = True
                code, msg = _systemctl("disable", "--now", UNIT_NAME)
                if code != 0:
                    problems.append(f"could not disable {UNIT_NAME}: {msg}")
                unit_path().unlink(missing_ok=True)
                _systemctl("daemon-reload")
            env_path().unlink(missing_ok=True)
    except OSError as e:
        problems.append(f"Qwen3-ASR unit update failed: {e}")
    return res


def vocab_phrases(sc: Any) -> list[str]:
    """Vocabulary phrases from a ``Sidecar`` or a raw metadata.json dict."""
    raw = getattr(sc, "vocabulary", None)
    if raw is None and isinstance(sc, dict):
        raw = sc.get("vocabulary", [])
    out: list[str] = []
    for v in raw or []:
        if isinstance(v, dict):
            out.append(str(v.get("phrase", "")))
        else:
            out.append(str(getattr(v, "phrase", v)))
    return [p for p in out if p]


def regenerate_hotwords(sc: Any) -> bool:
    """Rewrite only the hotwords env file from ``sc``'s vocabulary (no-op
    unless the Qwen3 marker is active). ``AppState.save`` already does
    this through :func:`apply_after_save`; this is for callers that saved
    the sidecar alone. Returns True when the file changed. The running
    server picks it up on its next (voxtype) restart."""
    if not qwen3_state(sc).get("active"):
        return False
    return _write_if_changed(env_path(), render_env(vocab_phrases(sc)))


def load_warnings(doc: Any, sc: Any, *, models_dir: Path | None = None) -> list[str]:
    """Problems worth a banner at load time (cheap: file checks only)."""
    out: list[str] = []
    w = xm.quantized_whisper_warning(doc)
    if w:
        out.append(w)
    block = qwen3_state(sc)
    if block.get("active") and not is_qwen3_active(doc, sc):
        out.append("Qwen3-ASR was active but config.toml no longer points at its "
                   "server — showing remote whisper; the server unit is removed on next save")
    elif is_qwen3_active(doc, sc):
        m = xm.get_extra(ENGINE_QWEN3, str(block.get("model") or ""))
        variant = str(block.get("variant") or "")
        if m is None or not xm.is_downloaded(ENGINE_QWEN3, m.name, models_dir):
            out.append("Qwen3-ASR is active but its model file is missing — voxtype "
                       "points at a dead endpoint; download it again or pick another model")
        elif variant not in xm.CRISPASR_BUILDS or not xm.build_installed(variant):
            out.append("Qwen3-ASR is active but its CrispASR engine is missing — voxtype "
                       "points at a dead endpoint; press Download on the model")
    return out


# --- status / health --------------------------------------------------------


def unit_status() -> dict[str, Any]:
    code, _ = 0, ""
    if os.environ.get("VOXTYPE_TUI_SYSTEMCTL") == "0" or shutil.which("systemctl") is None:
        return {"available": False}
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", UNIT_NAME, "-p", "ActiveState", "-p", "SubState",
             "-p", "MainPID", "-p", "UnitFileState", "-p", "NRestarts", "-p", "Result"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    try:
        pid = int(props.get("MainPID") or 0) or None
    except ValueError:
        pid = None
    return {
        "available": r.returncode == 0,
        "active_state": props.get("ActiveState"),
        "sub_state": props.get("SubState"),
        "main_pid": pid,
        "unit_file_state": props.get("UnitFileState"),
        "n_restarts": props.get("NRestarts"),
        "result": props.get("Result"),
    }


def probe_health(port: int, timeout: float = 0.5) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{endpoint_for(port)}/health", timeout=timeout) as r:
            body = r.read(512).decode("utf-8", "replace")
            if r.status != 200:
                return False, f"HTTP {r.status}"
            try:
                data = json.loads(body)
            except ValueError:
                return True, "ok"
            return data.get("status", "ok") == "ok", str(data.get("status", "ok"))
    except Exception as e:  # noqa: BLE001 — any failure is "unhealthy"
        return False, str(getattr(e, "reason", e))


def status(doc: Any, sc: Any, *, probe: bool = True) -> dict[str, Any]:
    """Engine/model line + Qwen3 server health, for status displays.

    ``{"engine", "engine_label", "model", "qwen3": {...}}`` where ``qwen3``
    is ``{"active": False}`` or ``{"active": True, "model", "port",
    "endpoint", "variant", "unit": unit_status(), "healthy", "detail"}``.
    ``detail`` is a short, human reason for an unhealthy server ("not
    running", "still starting", "port taken by another program", "not
    responding"); ``reason`` keeps the technical detail for logs."""
    engine, model = active_engine_model(doc, sc)
    out: dict[str, Any] = {"engine": engine, "engine_label": xm.engine_label(engine),
                           "model": model}
    if engine != ENGINE_QWEN3:
        out["qwen3"] = {"active": False}
        return out
    block = qwen3_state(sc)
    port = int(block["port"])
    q: dict[str, Any] = {"active": True, "model": block.get("model"), "port": port,
                         "endpoint": endpoint_for(port), "variant": block.get("variant")}
    if probe:
        unit = unit_status()
        healthy, reason = probe_health(port)
        detail = "ok" if healthy else "not responding"
        if not healthy:
            state = unit.get("active_state")
            if unit.get("available") and state in ("activating", "reloading"):
                detail = "still starting"
            elif unit.get("available") and state not in ("active", "activating"):
                result = unit.get("result")
                reason = f"unit {state}" + (f", {result}" if result not in (None, "success") else "")
                # Only blame the port when our server is fully stopped and some
                # other program is really listening there (a bind test would
                # also trip on TIME-WAIT sockets left by our own server).
                if state in ("inactive", "failed") and port_has_listener(port):
                    detail = "port taken by another program"
                    reason += f"; port {port} in use"
                else:
                    detail = "not running"
            elif state == "active":
                detail = "still starting"
        q.update(unit=unit, healthy=healthy, detail=detail, reason=reason)
        q.update(unit=unit, healthy=healthy, detail=detail)
    out["qwen3"] = q
    return out
