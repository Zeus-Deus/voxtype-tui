"""Usage counters, recent history and capture health.

Data flow (DESIGN A5, A13):

* ``voxtype-tui-postprocess`` (critical path) builds one record per
  dictation and calls :func:`enqueue`: a single ``O_APPEND`` line in
  ``$XDG_DATA_HOME/voxtype-tui/queue.jsonl`` under ``flock``. Nothing
  else — no pruning, no rewrites. Text is only in that line when recent
  history is on.
* :func:`maintain` drains the queue into ``usage.json`` (counters, never
  text) and ``history.jsonl`` (text, when history is on) and prunes
  expired history. It runs detached from the post-output hook and at the
  start of every read, so data is fresh even when that hook is not wired.
* Reads (:func:`summary`, :func:`list_items`) always enforce expiry.

Everything is idempotent by record id (ids are time-sortable): a crash
between writing ``usage.json`` and truncating the queue never double
counts. Files 0600 in a 0700 dir. Stdlib only.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keep the post-process import cost minimal
    from typing import Any, Iterator

from . import features as F

QUEUE_MAX_BYTES = 4 * 1024 * 1024
FAILURES_KEEP = 20
RECENT_IDS_KEEP = 2000
USAGE_VERSION = 1


# --- paths ---

def _dir(data_dir: Path | None = None) -> Path:
    return data_dir or F.data_dir()


def queue_path(data_dir: Path | None = None) -> Path:
    return _dir(data_dir) / "queue.jsonl"


def history_path(data_dir: Path | None = None) -> Path:
    return _dir(data_dir) / "history.jsonl"


def usage_path(data_dir: Path | None = None) -> Path:
    return _dir(data_dir) / "usage.json"


def health_path(data_dir: Path | None = None) -> Path:
    return _dir(data_dir) / "health.json"


def last_record_path() -> Path:
    return F.runtime_dir() / "last.json"


# --- locking ---

class _Lock:
    """Exclusive lock for maintenance and edits (``.lock``). The queue
    file's own flock (taken by :func:`enqueue`) is only ever held for
    microseconds by these writers (a rename, or a small in-place edit)."""

    def __init__(self, data_dir: Path | None = None):
        self.path = _dir(data_dir) / ".lock"
        self.fd = -1

    def __enter__(self):
        import fcntl

        F.ensure_private_dir(self.path.parent)
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl

        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


# --- record helpers ---

def new_id(now: float | None = None) -> str:
    now = time.time() if now is None else now
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    return f"{stamp}Z-{os.urandom(2).hex()}"


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime(ts)) + f"{int((ts % 1) * 1000):03d}Z"


def parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def count_words(text: str) -> int:
    return len(text.split())


# --- failures / health (rare paths only) ---

def note_failure(where: str, msg: str, data_dir: Path | None = None) -> None:
    """Append one failure line. Never raises (used on error paths)."""
    try:
        p = _dir(data_dir) / "failures.jsonl"
        F.ensure_private_dir(p.parent)
        line = json.dumps({"ts": iso(time.time()), "where": where, "msg": str(msg)[:300]})
        fd = os.open(str(p), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
    except Exception:
        pass


# --- critical path ---

def enqueue(record: dict, data_dir: Path | None = None) -> bool:
    """Append one record to the queue. Bounded: when the queue is over
    QUEUE_MAX_BYTES (nobody drained it for thousands of dictations) the
    record is dropped and counted. Returns True when written.

    The queue lock is only ever held by other writers for microseconds
    (maintenance renames the file away and drains the renamed copy), so
    this never waits on a drain. If the file was renamed between our open
    and our lock, we reopen the new one."""
    import fcntl

    p = queue_path(data_dir)
    F.ensure_private_dir(p.parent)
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    for _ in range(5):
        fd = os.open(str(p), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            st = os.fstat(fd)
            try:
                same = os.stat(str(p)).st_ino == st.st_ino
            except FileNotFoundError:
                same = False
            if not same:
                continue  # renamed away for draining: append to the new queue
            if st.st_size + len(line) > QUEUE_MAX_BYTES:
                dfd = os.open(str(p.parent / "dropped"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(dfd, b"1\n")
                finally:
                    os.close(dfd)
                return False
            os.write(fd, line)
            return True
        finally:
            os.close(fd)  # releases the flock
    return False


# --- storage ---

def _read_jsonl(path: Path) -> Iterator[dict]:
    try:
        f = path.open(encoding="utf-8")
    except OSError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def _write_jsonl(path: Path, items: list[dict]) -> None:
    F.atomic_write_text(
        path, "".join(json.dumps(i, ensure_ascii=False, separators=(",", ":")) + "\n" for i in items)
    )


def _load_usage(data_dir: Path | None = None) -> dict:
    try:
        u = json.loads(usage_path(data_dir).read_text(encoding="utf-8"))
        if isinstance(u, dict) and isinstance(u.get("days"), dict):
            return u
    except (OSError, ValueError):
        pass
    return {"version": USAGE_VERSION, "since": None, "recent_ids": [], "days": {}}


def _local_day_hour(ts: float) -> tuple[str, int]:
    lt = time.localtime(ts)
    return time.strftime("%Y-%m-%d", lt), lt.tm_hour


def _add_usage(u: dict, rec: dict) -> None:
    ts = parse_ts(rec.get("ts")) or time.time()
    day, hour = _local_day_hour(ts)
    d = u["days"].setdefault(day, {"n": 0, "w": 0, "c": 0, "md": 0, "mw": 0, "ms": 0.0, "h": {}, "m": {}})
    words = int(rec.get("words") or 0)
    d["n"] += 1
    d["w"] += words
    d["c"] += int(rec.get("chars") or 0)
    dur = rec.get("duration_s")
    if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0:
        d["md"] += 1
        d["mw"] += words
        d["ms"] = round(d["ms"] + float(dur), 3)
    d["h"][str(hour)] = d["h"].get(str(hour), 0) + 1
    key = f"{rec.get('engine') or 'unknown'}\t{rec.get('model') or 'unknown'}"
    d["m"][key] = d["m"].get(key, 0) + 1
    if not u.get("since") or day < u["since"]:
        u["since"] = day


def _history_tail_ids(path: Path, tail_bytes: int = 262144) -> set[str]:
    """Ids in the last ``tail_bytes`` of history.jsonl (dedupe window)."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return set()
    ids: set[str] = set()
    for line in tail.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("id"), str):
            ids.add(obj["id"])
    return ids


def _cutoff(keep_days: int | None, now: float | None = None) -> float | None:
    if keep_days is None:
        return None
    return (time.time() if now is None else now) - keep_days * 86400


def _expired(rec: dict, cutoff: float | None) -> bool:
    if cutoff is None:
        return False
    ts = parse_ts(rec.get("ts"))
    return ts is not None and ts < cutoff


def _draining_path(data_dir: Path | None) -> Path:
    return _dir(data_dir) / "queue.draining.jsonl"


def _claim_queue(data_dir: Path | None) -> None:
    """Rename queue.jsonl -> queue.draining.jsonl under the queue's flock
    (held for microseconds; enqueue then reopens a fresh queue file)."""
    import fcntl

    qp = queue_path(data_dir)
    try:
        qfd = os.open(str(qp), os.O_RDWR)
    except FileNotFoundError:
        return
    try:
        fcntl.flock(qfd, fcntl.LOCK_EX)
        if os.fstat(qfd).st_size == 0:
            return
        os.rename(str(qp), str(_draining_path(data_dir)))
    finally:
        os.close(qfd)


def maintain(data_dir: Path | None = None, feats: dict | None = None, now: float | None = None) -> dict:
    """Drain the queue into usage/history, prune expired history, update
    health. Idempotent (a crash mid-drain leaves queue.draining.jsonl,
    which the next run finishes; ids are deduped). Never blocks enqueue
    for longer than a rename. Returns a small report."""
    feats = feats or F.load()
    with _Lock(data_dir):
        return _maintain_locked(data_dir, feats, now)


def _maintain_locked(data_dir: Path | None, feats: dict, now: float | None = None) -> dict:
    report = {"drained": 0, "pruned": 0}
    hp = history_path(data_dir)
    dp = _draining_path(data_dir)
    for claim in (False, True):
        if claim:
            _claim_queue(data_dir)
        if not dp.exists():
            continue
        records = list(_read_jsonl(dp))
        if records:
            _drain(records, data_dir, feats, hp)
            report["drained"] += len(records)
        dp.unlink()
    report["pruned"] = _prune(hp, feats["history"]["keep_days"], now)
    return report


def _drain(records: list[dict], data_dir: Path | None, feats: dict, hp: Path) -> None:
    records.sort(key=lambda r: str(r.get("id", "")))
    u = _load_usage(data_dir)
    # Dedupe by id, not by ordering: a clock step backwards must not drop
    # records, and re-draining after a crash must not double count. The
    # whole batch stays in the window, however large it is.
    seen = [i for i in (u.get("recent_ids") or []) if isinstance(i, str)]
    seen_set = set(seen)
    changed_usage = False
    for rec in records:
        rid = str(rec.get("id", ""))
        if rec.get("count_usage") and rid and rid not in seen_set:
            _add_usage(u, rec)
            seen.append(rid)
            seen_set.add(rid)
            changed_usage = True
    u["recent_ids"] = seen[-max(RECENT_IDS_KEEP, len(records)):]
    u.pop("last_id", None)
    if changed_usage:
        F.atomic_write_text(usage_path(data_dir), json.dumps(u, separators=(",", ":")) + "\n")
    batch_bytes = sum(len(json.dumps(r)) for r in records)
    hist_ids = _history_tail_ids(hp, max(262144, 2 * batch_bytes))
    # Text is kept only when history was on at capture time AND is still on
    # now (turning it off drops queued text too).
    new_items = [
        _history_item(r) for r in records
        if feats["history"]["enabled"] and r.get("keep_text") and "final" in r
        and str(r.get("id", "")) not in hist_ids
    ]
    if new_items:
        F.ensure_private_dir(hp.parent)
        fd = os.open(str(hp), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, "".join(
                json.dumps(i, ensure_ascii=False, separators=(",", ":")) + "\n" for i in new_items
            ).encode())
        finally:
            os.close(fd)
    _update_health(data_dir, records[-1])


def _history_item(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in ("count_usage", "keep_text", "learned_hits")}


def _prune(hp: Path, keep_days: int | None, now: float | None) -> int:
    cutoff = _cutoff(keep_days, now)
    if cutoff is None or not hp.exists():
        return 0
    first = next(_read_jsonl(hp), None)
    if first is None or not _expired(first, cutoff):
        return 0
    items = list(_read_jsonl(hp))
    keep = [i for i in items if not _expired(i, cutoff)]
    _write_jsonl(hp, keep)
    return len(items) - len(keep)


def _update_health(data_dir: Path | None, newest: dict) -> None:
    h = _load_health(data_dir)
    if newest.get("ts"):
        h["last_capture_ts"] = newest["ts"]
    F.atomic_write_text(health_path(data_dir), json.dumps(h) + "\n")


def _load_health(data_dir: Path | None = None) -> dict:
    try:
        h = json.loads(health_path(data_dir).read_text(encoding="utf-8"))
        if isinstance(h, dict):
            return h
    except (OSError, ValueError):
        pass
    return {"last_capture_ts": None}


def health(data_dir: Path | None = None) -> dict:
    """{last_capture_ts, dropped, failures:[{ts,where,msg}], unavailable:[]}.
    ``unavailable`` is filled by :mod:`voxtype_tui.insights` (it needs the config)."""
    h = _load_health(data_dir)
    d = _dir(data_dir)
    try:
        dropped = sum(1 for _ in (d / "dropped").open())
    except OSError:
        dropped = 0
    failures = list(_read_jsonl(d / "failures.jsonl"))[-FAILURES_KEEP:]
    return {
        "last_capture_ts": h.get("last_capture_ts"),
        "dropped": dropped,
        "failures": failures,
        "unavailable": [],
    }


# --- read API ---

def summary(days: int = 365, data_dir: Path | None = None, feats: dict | None = None,
            now: float | None = None) -> dict:
    feats = feats or F.load()
    try:
        maintain(data_dir, feats, now)
    except Exception as e:
        note_failure("usage.summary", f"maintain: {e}", data_dir)
    from datetime import date, timedelta

    days = max(1, min(int(days), 3660))
    u = _load_usage(data_dir)
    today = date.fromtimestamp(time.time() if now is None else now)
    start = (today - timedelta(days=days - 1)).isoformat()
    end = today.isoformat()
    out_days = []
    hours = [0] * 24
    models: dict[str, int] = {}
    tot = {"n": 0, "w": 0, "md": 0, "mw": 0, "ms": 0.0}
    for day in sorted(u["days"]):
        if not (start <= day <= end):
            continue
        d = u["days"][day]
        out_days.append({"date": day, "dictations": d.get("n", 0), "words": d.get("w", 0),
                         "seconds": round(float(d.get("ms", 0.0)), 1)})
        for k in tot:
            tot[k] += d.get(k, 0)
        for hr, n in (d.get("h") or {}).items():
            if str(hr).isdigit() and 0 <= int(hr) < 24:
                hours[int(hr)] += n
        for key, n in (d.get("m") or {}).items():
            models[key] = models.get(key, 0) + n
    wpm_typing = feats["usage"]["typing_wpm"]
    measured_s = float(tot["ms"])
    wpm = round(tot["mw"] / (measured_s / 60.0), 1) if measured_s >= 10 else None
    saved = (
        round(max(0.0, tot["mw"] / wpm_typing * 60.0 - measured_s), 1) if tot["md"] else None
    )
    return {
        "enabled": bool(feats["usage"]["enabled"]),
        "days": out_days,
        "totals": {"dictations": tot["n"], "words": tot["w"], "seconds": round(measured_s, 1),
                   "wpm": wpm, "saved_seconds": saved},
        "hours": hours,
        "models": [
            {"engine": k.split("\t", 1)[0], "model": k.split("\t", 1)[-1], "dictations": n}
            for k, n in sorted(models.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "since": u.get("since"),
        "health": health(data_dir),
    }


def list_items(before: str | None = None, limit: int = 20, data_dir: Path | None = None,
               feats: dict | None = None, now: float | None = None) -> dict:
    feats = feats or F.load()
    try:
        maintain(data_dir, feats, now)
    except Exception as e:
        note_failure("history.list", f"maintain: {e}", data_dir)
    limit = max(1, min(int(limit), 50))
    keep_days = feats["history"]["keep_days"]
    cutoff = _cutoff(keep_days, now)
    items = [i for i in _read_jsonl(history_path(data_dir)) if not _expired(i, cutoff)]
    items.sort(key=lambda i: str(i.get("id", "")), reverse=True)
    if before:
        items = [i for i in items if str(i.get("id", "")) < before]
    page = items[:limit]
    return {
        "enabled": bool(feats["history"]["enabled"]),
        "keep_days": keep_days,
        "items": page,
        "next": page[-1]["id"] if len(items) > limit else None,
    }


def get(record_id: str, data_dir: Path | None = None) -> dict | None:
    """A record by id (history, queue or the runtime copy of the newest
    one). Expired records are never returned."""
    cutoff = _cutoff(F.load()["history"]["keep_days"])
    rec = _read_last()
    if rec and rec.get("id") == record_id and not _expired(rec, cutoff):
        return rec
    for item in _read_jsonl(history_path(data_dir)):
        if item.get("id") == record_id:
            return None if _expired(item, cutoff) else item
    for p in (queue_path(data_dir), _draining_path(data_dir)):
        for item in _read_jsonl(p):
            if item.get("id") == record_id and "final" in item and not _expired(item, cutoff):
                return _history_item(item)
    return None


def _read_last() -> dict | None:
    try:
        rec = json.loads(last_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def latest(max_age_s: float = 120.0) -> dict | None:
    """The newest dictation's full record (text included), for the
    post-output hook and the learning watcher. None when older than
    ``max_age_s`` or unavailable."""
    rec = _read_last()
    if not rec:
        return None
    ts = parse_ts(rec.get("ts"))
    if ts is None or time.time() - ts > max_age_s:
        return None
    return rec


def _strip_queue_text(data_dir: Path | None, record_id: str | None) -> int:
    """Drop text from queued (not yet drained) records, in place, holding
    the queue's flock only for this short edit. ``None`` = every record."""
    import fcntl

    try:
        qfd = os.open(str(queue_path(data_dir)), os.O_RDWR)
    except FileNotFoundError:
        return 0
    try:
        fcntl.flock(qfd, fcntl.LOCK_EX)
        with os.fdopen(os.dup(qfd), "r", encoding="utf-8") as f:
            items = []
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    items.append(obj)
        hits = 0
        out = []
        for i in items:
            if "final" in i and (record_id is None or i.get("id") == record_id):
                out.append(_strip_text(i))
                hits += 1
            else:
                out.append(i)
        if not hits:
            return 0
        data = "".join(json.dumps(i, ensure_ascii=False, separators=(",", ":")) + "\n" for i in out)
        os.ftruncate(qfd, 0)
        os.pwrite(qfd, data.encode(), 0)
        return hits
    finally:
        os.close(qfd)


def delete(record_id: str, data_dir: Path | None = None) -> int:
    with _Lock(data_dir):
        _maintain_locked(data_dir, F.load())  # no draining batch is left behind
        removed = 0
        hp = history_path(data_dir)
        items = list(_read_jsonl(hp))
        keep = [i for i in items if i.get("id") != record_id]
        if len(keep) != len(items):
            _write_jsonl(hp, keep)
            removed = 1
        if _strip_queue_text(data_dir, record_id):
            removed = 1  # enqueued meanwhile: keep its count, drop its text
        last = _read_last()
        if last and last.get("id") == record_id:
            try:
                last_record_path().unlink()
            except OSError:
                pass
        return removed


def _strip_text(rec: dict) -> dict:
    out = {k: v for k, v in rec.items() if k not in ("raw", "final")}
    out["keep_text"] = False
    return out


def clear(data_dir: Path | None = None) -> int:
    with _Lock(data_dir):
        _maintain_locked(data_dir, F.load())
        hp = history_path(data_dir)
        n = sum(1 for _ in _read_jsonl(hp))
        try:
            hp.unlink()
        except FileNotFoundError:
            pass
        n += _strip_queue_text(data_dir, None)
        try:
            last_record_path().unlink()
        except OSError:
            pass
        return n
