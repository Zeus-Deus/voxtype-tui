"""On/off switches for the insights features (usage, history, archive, learning).

Stored in ``$XDG_CONFIG_HOME/voxtype-tui/features.json`` (0600, dir 0700).
A missing file means "all defaults". Unknown keys are preserved on write so a
newer voxtype-tui (or L2's learning module) can add sections without an older
one dropping them.

``managed_hooks`` is the durable ownership record for the voxtype hook
commands we wrote into config.toml: ``{"output.pre_recording_command":
"<exact string>"}``. :mod:`voxtype_tui.hooks` only ever changes or removes
a hook whose current value equals the recorded string.

Stdlib only: this module is imported by the post-process CLI and the hook
entry point, which spawn on every dictation.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # typing costs startup time in the per-dictation hooks
    from typing import Any

VERSION = 1

DEFAULTS: "dict[str, Any]" = {
    "version": VERSION,
    "usage": {"enabled": True, "typing_wpm": 40},
    "history": {"enabled": True, "keep_days": 30},
    "archive": {"enabled": False, "dir": None, "save_audio": True, "save_text": True},
    "learning": {"enabled": True, "everywhere": False, "terminals": False, "notify": True},
}

KEEP_DAYS_CHOICES: tuple[int | None, ...] = (7, 30, 90, 365, None)
DEFAULT_ARCHIVE_DIR = "~/Documents/Voxtype"
MANAGED_HOOKS_KEY = "managed_hooks"


# --- paths (resolved at call time so HOME/XDG overrides always apply) ---

def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "voxtype-tui"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "voxtype-tui"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        import tempfile

        return Path(tempfile.gettempdir()) / f"voxtype-tui-{os.getuid()}"
    return Path(base) / "voxtype-tui"


def features_path() -> Path:
    return config_dir() / "features.json"


def ensure_private_dir(path: Path) -> Path:
    """mkdir -p with 0700 on the leaf (parents keep the umask)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if (path.stat().st_mode & 0o777) != 0o700:
            os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically with mode 0600."""
    import tempfile

    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- schema ---

def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _sanitize(d: dict) -> dict:
    """Replace invalid known values with defaults (load path: never raise)."""
    out = _merge(DEFAULTS, d)
    for section, defaults in DEFAULTS.items():
        if not isinstance(defaults, dict):
            continue
        if not isinstance(out.get(section), dict):
            out[section] = copy.deepcopy(defaults)
            continue
        for key, dv in defaults.items():
            try:
                _validate_value(section, key, out[section].get(key))
            except ValueError:
                out[section][key] = copy.deepcopy(dv)
    out["version"] = VERSION
    mh = out.get(MANAGED_HOOKS_KEY)
    if not isinstance(mh, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in mh.items()
    ):
        out[MANAGED_HOOKS_KEY] = {}
    return out


def _validate_value(section: str, key: str, value: Any) -> None:
    dv = DEFAULTS[section][key]
    if section == "history" and key == "keep_days":
        if value not in KEEP_DAYS_CHOICES or isinstance(value, bool):
            raise ValueError(f"history.keep_days must be one of {KEEP_DAYS_CHOICES}")
        return
    if section == "usage" and key == "typing_wpm":
        if not isinstance(value, int) or isinstance(value, bool) or not 10 <= value <= 200:
            raise ValueError("usage.typing_wpm must be an integer 10..200")
        return
    if section == "archive" and key == "dir":
        if value is None:
            return
        if not isinstance(value, str) or not value.strip():
            raise ValueError("archive.dir must be an absolute path or null")
        if not os.path.isabs(os.path.expanduser(value)):
            raise ValueError("archive.dir must be an absolute path")
        return
    if isinstance(dv, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{section}.{key} must be true or false")
        return


def validate(d: dict) -> None:
    """Raise ValueError when a known key holds an invalid value."""
    for section, defaults in DEFAULTS.items():
        if not isinstance(defaults, dict):
            continue
        sec = d.get(section)
        if not isinstance(sec, dict):
            raise ValueError(f"{section} must be an object")
        for key in defaults:
            _validate_value(section, key, sec.get(key))


# --- API ---

def _read_raw(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(path: Path | None = None) -> dict:
    """Features with defaults merged. Never raises; invalid values fall back
    to their defaults. Always contains ``managed_hooks`` (a dict)."""
    return _sanitize(_read_raw(path or features_path()))


def _write(d: dict, path: Path) -> None:
    atomic_write_text(path, json.dumps(d, indent=2, sort_keys=False) + "\n")


def _locked(path: Path):
    import fcntl

    ensure_private_dir(path.parent)
    fd = os.open(str(path.parent / ".features.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _unlock(fd: int) -> None:
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def update(
    patch: dict,
    path: Path | None = None,
    *,
    wire: bool = True,
    config_path: Path | None = None,
) -> dict:
    """Deep-merge ``patch`` into features.json, validate, write atomically.

    ``version`` and ``managed_hooks`` are not patchable here. Turning the
    archive on without a folder stores the documented default folder.
    With ``wire=True`` the voxtype hooks are reconciled afterwards
    (:func:`voxtype_tui.hooks.sync_config`); wiring failures never undo the
    features write — they surface as ``unavailable`` reasons.
    Returns the new features (as :func:`load` would)."""
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    patch = {k: v for k, v in patch.items() if k not in ("version", MANAGED_HOOKS_KEY)}
    path = path or features_path()
    fd = _locked(path)
    try:
        raw = _read_raw(path)
        current = _sanitize(raw)
        new = _merge(current, patch)
        validate(new)
        arch = new["archive"]
        if arch.get("enabled") and not arch.get("dir"):
            arch["dir"] = os.path.expanduser(DEFAULT_ARCHIVE_DIR)
        elif isinstance(arch.get("dir"), str):
            arch["dir"] = os.path.expanduser(arch["dir"])
        new["version"] = VERSION
        _write(new, path)
    finally:
        _unlock(fd)
    if wire:
        try:
            from . import hooks

            hooks.sync_config(config_path=config_path, features_file=path)
        except Exception:  # never undo the features write
            pass
    return load(path)


def set_managed_hooks(managed: dict[str, str], path: Path | None = None) -> None:
    """Replace the ownership record (hooks module only)."""
    path = path or features_path()
    fd = _locked(path)
    try:
        raw = _read_raw(path)
        if raw.get(MANAGED_HOOKS_KEY, {}) == managed and raw:
            return
        if not raw and not managed:
            return  # keep "missing file = defaults" until something is recorded
        raw[MANAGED_HOOKS_KEY] = dict(managed)
        raw.setdefault("version", VERSION)
        _write(raw, path)
    finally:
        _unlock(fd)


def public(feats: dict) -> dict:
    """Features without internal bookkeeping (for the UI)."""
    return {k: copy.deepcopy(v) for k, v in feats.items() if k != MANAGED_HOOKS_KEY}
