"""Per-dictation facts gathered cheaply around voxtype's hooks.

Used by the post-process CLI (critical path: stat + small reads only) and
by ``voxtype-tui-hook``. Stdlib only, no subprocesses.

* Recording start = mtime of voxtype's state file when the pre-recording
  hook sees ``recording`` (voxtype writes it before running the hook).
* Recording stop = mtime of the state file when post-process runs (it was
  rewritten to ``transcribing`` at stop). Both come from the daemon's own
  writes, so the duration is measured, never guessed; anything implausible
  becomes ``None``.
* Engine/model/language come from config.toml, parsed once per config
  change and cached in the runtime dir. ``model_uncertain`` is True when
  config.toml is newer than the running daemon (voxtype's pid file).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import features as F

DEFAULT_MAX_DURATION = 60


def voxtype_runtime() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "voxtype"


def state_file() -> Path:
    return voxtype_runtime() / "state"


def read_state() -> tuple[str, float | None]:
    p = state_file()
    try:
        with p.open() as f:
            st = os.fstat(f.fileno())
            return f.read(32).strip(), st.st_mtime
    except OSError:
        return "", None


def rec_start_path() -> Path:
    return F.runtime_dir() / "rec-start.json"


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "voxtype" / "config.toml"


def _engine_model(doc: dict) -> tuple[str, str, str | None]:
    engine = str(doc.get("engine") or "whisper")
    section = doc.get(engine) if isinstance(doc.get(engine), dict) else {}
    model_key = "model_type" if engine == "parakeet" else "model"
    model = section.get(model_key) if isinstance(section, dict) else None
    if engine == "whisper":
        w = doc.get("whisper") or {}
        model = w.get("model") or "base.en"
        mode = w.get("mode")
        if mode == "remote" or (w.get("backend") == "remote"):
            model = w.get("remote_model") or "remote"
    lang = (doc.get("whisper") or {}).get("language") if engine == "whisper" else None
    return engine, str(model or "default"), (str(lang) if lang else None)


def config_info(cfg: Path | None = None) -> dict:
    """{engine, model, language, max_duration_secs, model_uncertain}.
    Parses config.toml only when it changed since the cached parse."""
    cfg = cfg or config_path()
    try:
        st = cfg.stat()
    except OSError:
        return {"engine": None, "model": None, "language": None,
                "max_duration_secs": DEFAULT_MAX_DURATION, "model_uncertain": True}
    key = f"{st.st_mtime_ns}:{st.st_size}:{st.st_ino}"
    cache = F.runtime_dir() / "config-cache.json"
    info = None
    try:
        c = json.loads(cache.read_text())
        if c.get("key") == key and c.get("path") == str(cfg):
            info = c["info"]
    except (OSError, ValueError, KeyError, TypeError):
        info = None
    if info is None:
        import tomllib

        try:
            with cfg.open("rb") as f:
                doc = tomllib.load(f)
        except (OSError, ValueError):
            doc = {}
        engine, model, lang = _engine_model(doc)
        audio = doc.get("audio") or {}
        info = {
            "engine": engine, "model": model, "language": lang,
            "max_duration_secs": int(audio.get("max_duration_secs") or DEFAULT_MAX_DURATION),
            "device": str(audio.get("device") or "default"),
        }
        try:
            F.atomic_write_text(cache, json.dumps({"key": key, "path": str(cfg), "info": info}))
        except OSError:
            pass
    out = dict(info)
    try:
        daemon_start = (voxtype_runtime() / "pid").stat().st_mtime
        out["model_uncertain"] = st.st_mtime > daemon_start
    except OSError:
        out["model_uncertain"] = True
    return out


def read_rec_start(max_age_s: float = 3600.0) -> dict | None:
    try:
        info = json.loads(rec_start_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    start = info.get("start")
    if not isinstance(start, (int, float)) or time.time() - start > max_age_s:
        return None
    return info


def measure_duration(start_info: dict | None, max_duration: int) -> tuple[float | None, float | None]:
    """(started_at_epoch, duration_s) or (None, None) when not measurable."""
    if not start_info:
        return None, None
    start = start_info.get("start")
    state, mtime = read_state()
    if not isinstance(start, (int, float)) or mtime is None:
        return None, None
    if state == "recording":
        return None, None
    dur = mtime - start
    if dur <= 0 or dur > max_duration + 10:
        return None, None
    return float(start), round(dur, 2)
