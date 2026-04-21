"""Load, mutate, and atomically save ~/.config/voxtype/config.toml.

Comments and formatting are preserved via tomlkit. The file is Voxtype's
source of truth — the sidecar only decorates it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import TOMLDocument

CONFIG_PATH = Path.home() / ".config" / "voxtype" / "config.toml"
BACKUP_SUFFIX = ".voxtype-tui-bak"


class ValidationError(RuntimeError):
    """Raised when `voxtype -c <tmp> config` rejects a proposed write."""


def _backup_path(path: Path) -> Path:
    return path.parent / (path.name + BACKUP_SUFFIX)


def validate_with_voxtype(path: Path, timeout: float = 10.0) -> tuple[bool, str]:
    """Run `voxtype -c <path> config` as a pre-save sanity check.

    Returns (ok, message). When the voxtype binary isn't installed we return
    (True, "") so the save proceeds — this is primarily protection against
    our own bad writes, and CI environments without voxtype shouldn't block.
    """
    if shutil.which("voxtype") is None:
        return True, "voxtype binary not found; validation skipped"
    try:
        result = subprocess.run(
            ["voxtype", "-c", str(path), "config"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "voxtype validation timed out"
    except OSError as e:
        return False, f"could not invoke voxtype: {e}"
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or result.stdout).strip()


async def safe_save_async(
    doc: TOMLDocument,
    path: Path = CONFIG_PATH,
    *,
    validate: bool = True,
    backup: bool = True,
) -> None:
    """Non-blocking equivalent of safe_save. Use from Textual/async contexts.
    The validation subprocess runs on a worker thread via asyncio.to_thread."""
    await asyncio.to_thread(safe_save, doc, path, validate=validate, backup=backup)


def safe_save(
    doc: TOMLDocument,
    path: Path = CONFIG_PATH,
    *,
    validate: bool = True,
    backup: bool = True,
) -> None:
    """Atomic save with pre-write voxtype validation and first-write backup.

    Flow:
      1. Serialize the doc to a temp file in the same directory.
      2. Run `voxtype -c <tmp> config`. On non-zero, delete tmp and raise.
      3. If the destination exists and the backup file does not, copy the
         existing destination to `<name>.voxtype-tui-bak`. This happens at
         most once per destination path.
      4. `os.replace(tmp, path)` — atomic on POSIX.

    The original file is never touched until step 4.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(tomlkit.dumps(doc))

        if validate:
            ok, msg = validate_with_voxtype(tmp)
            if not ok:
                raise ValidationError(
                    f"refusing to save: voxtype rejected the proposed config.\n{msg}"
                )

        if backup and path.exists():
            bak = _backup_path(path)
            if not bak.exists():
                shutil.copy2(path, bak)

        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise

# Fields where a change requires `systemctl --user restart voxtype` to take
# effect. Confirmed by static analysis of /usr/bin/voxtype (explicit restart
# prompts around model/engine) and by /proc/PID/fd probe on the running daemon
# (input devices and inotify watches are resident resources set at startup).
RESTART_SENSITIVE_PATHS: frozenset[str] = frozenset({
    "state_file",
    "engine",
    "whisper.model",
    "whisper.mode",
    "whisper.backend",
    "whisper.gpu_isolation",
    "whisper.remote_endpoint",
    "parakeet.model_type",
    "moonshine.model",
    "sensevoice.model",
    "paraformer.model",
    "dolphin.model",
    "omnilingual.model",
    "hotkey.key",
    "hotkey.modifiers",
    "hotkey.mode",
    "hotkey.enabled",
    "audio.device",
    "audio.sample_rate",
    "audio.feedback.enabled",
    "audio.feedback.theme",
    "audio.feedback.volume",
    "vad.enabled",
    "vad.model",
    "vad.threshold",
})


def load(path: Path = CONFIG_PATH) -> TOMLDocument:
    return tomlkit.parse(path.read_text())


def save_atomic(doc: TOMLDocument, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(tomlkit.dumps(doc))
        os.replace(tmp, path)
    except Exception:
        if Path(tmp).exists():
            os.unlink(tmp)
        raise


def get_initial_prompt(doc: TOMLDocument) -> str | None:
    whisper = doc.get("whisper")
    if whisper is None:
        return None
    val = whisper.get("initial_prompt")
    return str(val) if val is not None else None


def set_initial_prompt(doc: TOMLDocument, value: str | None) -> None:
    if "whisper" not in doc:
        doc["whisper"] = tomlkit.table()
    whisper = doc["whisper"]
    if not value:
        if "initial_prompt" in whisper:
            del whisper["initial_prompt"]
        return
    whisper["initial_prompt"] = value


def get_replacements(doc: TOMLDocument) -> dict[str, str]:
    text = doc.get("text")
    if text is None:
        return {}
    reps = text.get("replacements")
    if reps is None:
        return {}
    return {str(k): str(v) for k, v in reps.items()}


def set_replacements(doc: TOMLDocument, reps: dict[str, str]) -> None:
    """Reconcile the `[text].replacements` table to match `reps` exactly.

    Done incrementally — keys not in `reps` are deleted, others are added or
    updated. This preserves any comments the user attached to unchanged
    entries (which a wipe-and-replace would destroy).
    """
    text = doc.get("text")

    if not reps:
        if text is not None and "replacements" in text:
            del text["replacements"]
        if text is not None and len(text) == 0:
            del doc["text"]
        return

    if "text" not in doc:
        doc["text"] = tomlkit.table()
        text = doc["text"]
    if "replacements" not in text:
        # Fresh materialization — inline form is compact and matches the
        # default template's commented-out hint.
        inline = tomlkit.inline_table()
        for k, v in reps.items():
            inline[k] = v
        text["replacements"] = inline
        return

    existing = text["replacements"]
    for key in list(existing.keys()):
        if key not in reps:
            del existing[key]
    for k, v in reps.items():
        if k not in existing or existing[k] != v:
            existing[k] = v


def add_replacement(doc: TOMLDocument, from_text: str, to_text: str) -> None:
    reps = get_replacements(doc)
    reps[from_text] = to_text
    set_replacements(doc, reps)


def remove_replacement(doc: TOMLDocument, from_text: str) -> None:
    reps = get_replacements(doc)
    reps.pop(from_text, None)
    set_replacements(doc, reps)


def _get_in(node: Any, path: str) -> Any:
    cur = node
    for part in path.split("."):
        if cur is None or not hasattr(cur, "get"):
            return None
        cur = cur.get(part)
    return cur


def diff_restart_sensitive(old: TOMLDocument, new: TOMLDocument) -> list[str]:
    return sorted(
        p for p in RESTART_SENSITIVE_PATHS
        if _get_in(old, p) != _get_in(new, p)
    )
