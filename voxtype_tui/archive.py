"""Archive: your own dataset of dictations (audio + transcript + metadata).

Off by default. Layout under the folder the user chose (default
``~/Documents/Voxtype``)::

    <dir>/YYYY/MM/DD/<id>.wav    # separate recording of your mic (16 kHz mono s16)
    <dir>/YYYY/MM/DD/<id>.json   # the record (+ text when "save text" is on)
    <dir>/index.jsonl            # one line per dictation

The WAV is a *separate* recording of the configured ``audio.device``
started by the pre-recording hook, not voxtype's exact ASR input; its
JSON says so and carries the measured offset between voxtype's recording
start and the recorder's start. Finalization runs detached from the
post-output hook: it waits for the recorder to exit (WAV header
finalized), moves the WAV, then writes the JSON and the index line. On any
failure the temp WAV is removed and the failure is recorded in health.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import wave
from pathlib import Path

from . import capture, history
from . import features as F

AUDIO_NOTE = "separate recording of your mic"


def archive_dir(feats: dict) -> Path:
    d = feats["archive"].get("dir") or F.DEFAULT_ARCHIVE_DIR
    return Path(os.path.expanduser(d))


def _rec_used(record_id: str) -> dict | None:
    try:
        info = json.loads(capture.rec_used_path(record_id).read_text())
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def _wait_exit(pid: int, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    return False


def _mkdirs_private(path: Path) -> None:
    """Create missing dirs as 0700; never chmod a folder that already exists
    (the archive root may be a folder the user picked)."""
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            pass


def _rel_day(ts: float) -> str:
    return time.strftime("%Y/%m/%d", time.localtime(ts))


def finalize(rec: dict, feats: dict, timeout: float = 15.0) -> dict | None:
    """Write the archive entry for ``rec``. Returns the index line or None."""
    arch = feats["archive"]
    root = archive_dir(feats)
    rid = rec["id"]
    ts = history.parse_ts(rec.get("ts")) or time.time()
    rel = _rel_day(ts)
    if (root / rel / f"{rid}.json").exists():
        return None  # already archived (claim once; never overwrite)
    info = _rec_used(rid)
    wav_src = None
    if info and info.get("wav") and rec.get("started_at") is not None:
        if abs(float(info.get("start", 0)) - (history.parse_ts(rec["started_at"]) or 0)) < 0.01:
            wav_src = Path(info["wav"])
    entry: dict = {"id": rid, "ts": rec.get("ts"), "json": f"{rel}/{rid}.json", "audio": None,
                   "words": rec.get("words"), "duration_s": rec.get("duration_s"),
                   "engine": rec.get("engine"), "model": rec.get("model")}
    meta = {k: v for k, v in rec.items() if k not in ("learned_hits",)}
    if not arch.get("save_text"):
        meta.pop("raw", None)
        meta.pop("final", None)
    meta["audio"] = None
    day_dir = root / rel
    try:
        if wav_src is not None and arch.get("save_audio"):
            pid = int(info.get("recorder_pid") or 0)
            if pid and not _wait_exit(pid, timeout):
                raise RuntimeError("recorder did not stop in time")
            with wave.open(str(wav_src), "rb") as w:
                frames, rate = w.getnframes(), w.getframerate()
            if frames <= 0:
                raise RuntimeError("recorded WAV is empty")
            _mkdirs_private(day_dir)
            dst = day_dir / f"{rid}.wav"
            shutil.move(str(wav_src), str(dst))
            os.chmod(dst, 0o600)
            meta["audio"] = f"{rel}/{rid}.wav"
            entry["audio"] = meta["audio"]
            start = float(info["start"])
            meta["audio_info"] = {
                "note": AUDIO_NOTE,
                "device": info.get("device", "default"),
                "source": info.get("source"),
                "source_note": info.get("source_note"),
                "recorder": info.get("recorder"),
                "sample_rate": rate,
                "seconds": round(frames / rate, 3),
                # recorder process start minus voxtype's recording start
                "start_offset_s": round(float(info.get("recorder_started", start)) - start, 3),
            }
        _mkdirs_private(day_dir)
        F.atomic_write_text(day_dir / f"{rid}.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        idx = root / "index.jsonl"
        fd = os.open(str(idx), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(entry, ensure_ascii=False) + "\n").encode())
        finally:
            os.close(fd)
        return entry
    finally:
        if wav_src is not None and wav_src.exists():
            try:
                wav_src.unlink()
            except OSError:
                pass
        try:
            capture.rec_used_path(rid).unlink()  # only this record's handoff
        except OSError:
            pass
