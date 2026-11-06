"""Insights data layer: features.json, usage/history queue, hook wiring,
post-process capture, v4 migration (DESIGN A3–A13)."""
from __future__ import annotations

import io
import json
import multiprocessing
import os
import stat
import time
from pathlib import Path

import pytest
import tomlkit

from voxtype_tui import (
    capture, cli_postprocess, config, features, history, hooks, insights, migrations,
    sidecar, sync,
)
from voxtype_tui.state import AppState


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        d = tmp_path / name.lower()
        d.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(d))
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_learning_watcher(monkeypatch: pytest.MonkeyPatch):
    """Tests decide whether L2's watcher module exists (it changes whether
    post_output is wired for learning)."""
    real = hooks._module_exists
    monkeypatch.setattr(hooks, "_module_exists",
                        lambda name: False if name == "voxtype_tui.learn_watch" else real(name))


@pytest.fixture
def live_config(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config.toml that AppState treats as the daemon's live config."""
    cfg = xdg / "xdg_config_home" / "voxtype" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[hotkey]\nenabled = false\n\n[whisper]\nmodel = "base.en"\n\n[output]\nmode = "type"\n')
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    monkeypatch.setattr(sidecar, "SIDECAR_PATH", xdg / "xdg_config_home" / "voxtype-tui" / "metadata.json")
    return cfg


def _rec(i: int, *, ts: float | None = None, words: int = 10, dur: float | None = 5.0,
         usage: bool = True, text: bool = True) -> dict:
    ts = time.time() if ts is None else ts
    r = {"id": history.new_id(ts)[:-4] + f"{i:04x}", "ts": history.iso(ts), "duration_s": dur,
         "words": words, "chars": words * 5, "engine": "whisper", "model": "base.en",
         "count_usage": usage, "keep_text": text}
    if text:
        r.update(raw=f"raw {i}", final=f"final {i}")
    return r


# --- features ---------------------------------------------------------------

def test_features_defaults_when_missing(xdg):
    f = features.load()
    assert f["usage"] == {"enabled": True, "typing_wpm": 40}
    assert f["history"] == {"enabled": True, "keep_days": 30}
    assert f["archive"]["enabled"] is False and f["archive"]["dir"] is None
    assert f["learning"]["enabled"] is True and f["learning"]["everywhere"] is False
    assert f["managed_hooks"] == {}
    assert not features.features_path().exists()


def test_features_update_merges_validates_and_is_private(xdg):
    p = features.features_path()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"version": 1, "future": {"x": 1}, "learning": {"keep_context": True}}))
    f = features.update({"history": {"keep_days": 90}}, wire=False)
    assert f["history"] == {"enabled": True, "keep_days": 90}
    raw = json.loads(p.read_text())
    assert raw["future"] == {"x": 1}  # unknown keys preserved
    assert raw["learning"]["keep_context"] is True
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    for bad in ({"history": {"keep_days": 12}}, {"usage": {"enabled": "yes"}},
                {"usage": {"typing_wpm": 5}}, {"archive": {"dir": "relative/path"}}):
        with pytest.raises(ValueError):
            features.update(bad, wire=False)
    assert features.load()["history"]["keep_days"] == 90  # unchanged after refusals
    f = features.update({"managed_hooks": {"x": "y"}, "version": 9}, wire=False)
    assert f["managed_hooks"] == {} and f["version"] == 1


def test_archive_on_without_dir_uses_documented_default(xdg, monkeypatch):
    monkeypatch.setenv("HOME", str(xdg / "home"))
    f = features.update({"archive": {"enabled": True}}, wire=False)
    assert f["archive"]["dir"] == str(xdg / "home" / "Documents" / "Voxtype")


def test_features_invalid_file_falls_back_to_defaults(xdg):
    p = features.features_path()
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert features.load()["usage"]["enabled"] is True
    p.write_text(json.dumps({"history": {"keep_days": "lots"}, "usage": []}))
    f = features.load()
    assert f["history"]["keep_days"] == 30 and f["usage"]["enabled"] is True


# --- history / usage --------------------------------------------------------

def test_enqueue_and_maintain_counts_usage_and_keeps_history(xdg):
    for i in range(3):
        assert history.enqueue(_rec(i, words=12, dur=6.0))
    s = history.summary(30)
    assert s["totals"]["dictations"] == 3 and s["totals"]["words"] == 36
    assert s["totals"]["seconds"] == 18.0
    assert s["totals"]["wpm"] == 120.0  # 36 words / 0.3 min
    # typing 36 words at 40 wpm = 54 s; spoke 18 s -> 36 s saved
    assert s["totals"]["saved_seconds"] == 36.0
    assert sum(s["hours"]) == 3 and s["models"] == [
        {"engine": "whisper", "model": "base.en", "dictations": 3}]
    assert s["health"]["last_capture_ts"] is not None
    page = history.list_items(limit=2)
    assert [i["final"] for i in page["items"]] == ["final 2", "final 1"]
    assert page["next"] is not None
    rest = history.list_items(before=page["next"], limit=2)
    assert [i["final"] for i in rest["items"]] == ["final 0"] and rest["next"] is None
    assert not history.queue_path().exists()  # drained (renamed away, then removed)
    for p in (history.usage_path(), history.history_path()):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_unmeasured_duration_is_null_not_guessed(xdg):
    history.enqueue(_rec(1, words=10, dur=None))
    s = history.summary(30)
    assert s["totals"]["dictations"] == 1
    assert s["totals"]["seconds"] == 0 and s["totals"]["wpm"] is None
    assert s["totals"]["saved_seconds"] is None


def test_drain_is_idempotent_after_crash_before_truncate(xdg):
    history.enqueue(_rec(1))
    history.enqueue(_rec(2))
    q = history.queue_path().read_bytes()
    history.maintain()
    history.queue_path().write_bytes(q)  # simulate: crashed before truncate
    history.maintain()
    s = history.summary(30)
    assert s["totals"]["dictations"] == 2
    assert len(history.list_items()["items"]) == 2


def test_usage_counts_with_history_off_and_no_text_stored(xdg):
    features.update({"history": {"enabled": False}}, wire=False)
    history.enqueue(_rec(1, text=False))
    s = history.summary(30)
    assert s["totals"]["dictations"] == 1
    assert not history.history_path().exists()
    assert history.list_items()["items"] == []


def test_history_off_drops_already_queued_text(xdg):
    history.enqueue(_rec(1, text=True))
    features.update({"history": {"enabled": False}}, wire=False)
    history.maintain()
    assert not history.history_path().exists()
    assert history.summary(30)["totals"]["dictations"] == 1


def test_expiry_enforced_on_read_and_pruned_on_maintain(xdg):
    old = time.time() - 40 * 86400
    history.enqueue(_rec(1, ts=old))
    history.enqueue(_rec(2))
    items = history.list_items()["items"]
    assert [i["final"] for i in items] == ["final 2"]
    lines = history.history_path().read_text().splitlines()
    assert len(lines) == 1  # pruned on disk by maintain
    features.update({"history": {"keep_days": None}}, wire=False)
    history.enqueue(_rec(3, ts=old - 400 * 86400))
    assert len(history.list_items()["items"]) == 2  # forever keeps old items


def test_summary_window_and_hours(xdg):
    now = time.time()
    history.enqueue(_rec(1, ts=now - 10 * 86400))
    history.enqueue(_rec(2, ts=now))
    assert history.summary(7)["totals"]["dictations"] == 1
    s = history.summary(365)
    assert s["totals"]["dictations"] == 2 and len(s["days"]) == 2
    assert s["since"] == time.strftime("%Y-%m-%d", time.localtime(now - 10 * 86400))


def test_delete_and_clear(xdg):
    for i in range(3):
        history.enqueue(_rec(i))
    ids = [i["id"] for i in history.list_items()["items"]]
    assert history.delete(ids[0]) == 1
    assert history.delete("nope") == 0
    assert len(history.list_items()["items"]) == 2
    history.enqueue(_rec(9))
    assert history.clear() == 3
    assert history.list_items()["items"] == []
    assert history.summary(30)["totals"]["dictations"] == 4  # counters stay


def _enqueue_many(n: int) -> None:
    for i in range(n):
        history.enqueue(_rec(i, words=1) | {"pid": os.getpid()})


def test_concurrent_enqueue_keeps_every_line(xdg):
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_enqueue_many, args=(50,)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    lines = history.queue_path().read_text().splitlines()
    assert len(lines) == 200
    assert all(json.loads(line)["words"] == 1 for line in lines)


def test_queue_is_bounded(xdg, monkeypatch):
    monkeypatch.setattr(history, "QUEUE_MAX_BYTES", 600)
    written = sum(history.enqueue(_rec(i)) for i in range(20))
    assert 0 < written < 20
    assert history.health()["dropped"] == 20 - written


def test_summary_one_year_is_fast(xdg):
    u = {"version": 1, "since": "2025-11-06", "last_id": "z", "days": {}}
    base = time.time()
    for d in range(366):
        day = time.strftime("%Y-%m-%d", time.localtime(base - d * 86400))
        u["days"][day] = {"n": 20, "w": 600, "c": 3000, "md": 20, "mw": 600, "ms": 200.0,
                          "h": {str(h): 1 for h in range(20)}, "m": {"whisper\tbase.en": 20}}
    features.atomic_write_text(history.usage_path(), json.dumps(u))
    t = time.perf_counter()
    s = history.summary(365)
    ms = (time.perf_counter() - t) * 1000
    assert s["totals"]["dictations"] == 365 * 20
    assert ms < 50, ms


# --- hooks ------------------------------------------------------------------

def _doc(text: str = "[output]\nmode = \"type\"\n"):
    return tomlkit.parse(text)


def test_reconcile_wires_needed_hooks_and_is_idempotent(xdg):
    doc = _doc()
    plan = hooks.reconcile(doc, features.load())
    assert plan.changed
    assert doc["output"]["pre_recording_command"] == hooks.OUR_COMMANDS[hooks.PRE_RECORDING]
    assert "post_output_command" not in doc["output"]  # archive off, no watcher module
    assert doc["output"]["post_process"]["command"] == config.POSTPROCESS_COMMAND
    assert plan.managed == {hooks.PRE_RECORDING: hooks.OUR_COMMANDS[hooks.PRE_RECORDING]}
    feats = features.load() | {"managed_hooks": plan.managed}
    before = tomlkit.dumps(doc)
    again = hooks.reconcile(doc, feats)
    assert not again.changed and tomlkit.dumps(doc) == before


def test_learning_watcher_wires_post_output_only_when_installed(xdg, monkeypatch):
    doc = _doc()
    monkeypatch.setattr(hooks, "_module_exists", lambda name: True)
    hooks.reconcile(doc, features.load())
    assert doc["output"]["post_output_command"] == hooks.OUR_COMMANDS[hooks.POST_OUTPUT]
    feats = features.load()
    feats["learning"]["enabled"] = False
    feats["managed_hooks"] = {hooks.POST_OUTPUT: hooks.OUR_COMMANDS[hooks.POST_OUTPUT]}
    hooks.reconcile(doc, feats)
    assert "post_output_command" not in doc["output"]


def test_reconcile_archive_wires_post_output(xdg):
    doc = _doc()
    feats = features.load()
    feats["archive"]["enabled"] = True
    hooks.reconcile(doc, feats)
    assert doc["output"]["post_output_command"] == hooks.OUR_COMMANDS[hooks.POST_OUTPUT]


def test_reconcile_never_touches_foreign_hooks(xdg):
    doc = _doc('[output]\npre_recording_command = "hyprctl dispatch submap dictate"\n'
               'post_output_command = "hyprctl dispatch submap reset"\n'
               '[output.post_process]\ncommand = "ollama run x"\n')
    feats = features.load()
    feats["archive"]["enabled"] = True
    before = tomlkit.dumps(doc)
    plan = hooks.reconcile(doc, feats)
    assert tomlkit.dumps(doc) == before and not plan.changed
    assert plan.unavailable["archive"] == "your own pre_recording_command is set"
    assert plan.unavailable["usage"] == "your own post-processing command is set"
    assert plan.unavailable["history"] == "your own post-processing command is set"
    # turning features off must not remove the user's commands either
    feats = features.load()
    for k in ("usage", "history", "learning"):
        feats[k]["enabled"] = False
    hooks.reconcile(doc, feats)
    assert tomlkit.dumps(doc) == before


def test_reconcile_unwires_only_our_hooks_when_off(xdg):
    doc = _doc()
    feats = features.load()
    feats["archive"]["enabled"] = True
    plan = hooks.reconcile(doc, feats)
    off = features.load() | {"managed_hooks": plan.managed}
    for k in ("usage", "history", "archive", "learning"):
        off[k]["enabled"] = False
    plan2 = hooks.reconcile(doc, off)
    assert plan2.changed and plan2.managed == {}
    assert "pre_recording_command" not in doc["output"]
    assert "post_output_command" not in doc["output"]


def test_reconcile_upgrades_a_recorded_older_string(xdg):
    old = "voxtype-tui-hook pre-recording"
    doc = _doc(f'[output]\npre_recording_command = "{old}"\n')
    feats = features.load() | {"managed_hooks": {hooks.PRE_RECORDING: old}}
    plan = hooks.reconcile(doc, feats)
    assert doc["output"]["pre_recording_command"] == hooks.OUR_COMMANDS[hooks.PRE_RECORDING]
    assert plan.managed[hooks.PRE_RECORDING] == hooks.OUR_COMMANDS[hooks.PRE_RECORDING]


def test_wired_command_is_uninstall_safe():
    import subprocess

    env = {"PATH": "/usr/bin:/bin"}  # no voxtype-tui-hook on this PATH
    for cmd in hooks.OUR_COMMANDS.values():
        r = subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, timeout=5)
        assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""


def test_sync_config_and_unwire_all_roundtrip(live_config):
    original = live_config.read_text()
    res = hooks.sync_config()
    assert res["changed"]
    text = live_config.read_text()
    assert "voxtype-tui-hook pre-recording" in text
    assert features.load()["managed_hooks"] == {
        hooks.PRE_RECORDING: hooks.OUR_COMMANDS[hooks.PRE_RECORDING]}
    assert hooks.sync_config()["changed"] is False
    assert hooks.unwire_all()["changed"] is True
    text = live_config.read_text()
    assert "voxtype-tui-hook" not in text
    assert "pre_recording_command" not in text
    assert features.load()["managed_hooks"] == {}
    assert "[output]" in original


def test_features_update_rewires_config(live_config):
    insights.set_features({"archive": {"enabled": True, "dir": str(live_config.parent / "arch")}})
    assert "voxtype-tui-hook post-output" in live_config.read_text()
    out = insights.set_features({"archive": {"enabled": False}})
    assert "post_output_command" not in live_config.read_text()
    assert out["restart_needed"] is True
    assert out["available"]["usage"] is True


# --- post-process capture ----------------------------------------------------

def _run_main(monkeypatch, text: str) -> str:
    out = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    monkeypatch.setattr("sys.stdout", out)
    assert cli_postprocess.main() == 0
    return out.getvalue()


def test_postprocess_captures_a_record(xdg, monkeypatch, tmp_path):
    monkeypatch.setattr(cli_postprocess, "SIDECAR_PATH", tmp_path / "none.json")
    cfg = Path(os.environ["XDG_CONFIG_HOME"]) / "voxtype" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[whisper]\nmodel = "base.en"\nlanguage = "en"\n')
    old = time.time() - 100
    os.utime(cfg, (old, old))
    vt = Path(os.environ["XDG_RUNTIME_DIR"]) / "voxtype"
    vt.mkdir()
    (vt / "pid").write_text("1\n")
    state = vt / "state"
    state.write_text("recording")
    start = time.time() - 8.0
    os.utime(state, (start, start))
    features.ensure_private_dir(features.runtime_dir())
    features.atomic_write_text(capture.rec_start_path(), json.dumps(
        {"start": start, "app": {"class": "foot", "title": "t", "pid": 1, "address": "0x1"}}))
    state.write_text("transcribing")
    stop = start + 7.5
    os.utime(state, (stop, stop))
    assert _run_main(monkeypatch, "hello there world") == "hello there world"
    item = history.list_items()["items"][0]
    assert item["raw"] == item["final"] == "hello there world"
    assert item["words"] == 3 and item["duration_s"] == 7.5
    assert item["app"]["class"] == "foot"
    assert item["engine"] == "whisper" and item["model"] == "base.en"
    assert item["language"] == "en" and item["model_uncertain"] is False
    assert item["changed_by"] == []
    assert not capture.rec_start_path().exists()  # consumed, never reused


def test_postprocess_capture_failure_never_changes_output(xdg, monkeypatch, tmp_path):
    monkeypatch.setattr(cli_postprocess, "SIDECAR_PATH", tmp_path / "none.json")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(history, "enqueue", boom)
    assert _run_main(monkeypatch, "keep me exactly") == "keep me exactly"
    fails = history.health()["failures"]
    assert fails and fails[-1]["where"] == "post_process"


def test_postprocess_pipeline_failure_passes_text_through(xdg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(cli_postprocess, "process_detailed", boom)
    assert _run_main(monkeypatch, "raw text") == "raw text"


def test_all_features_off_writes_nothing(xdg, monkeypatch, tmp_path):
    monkeypatch.setattr(cli_postprocess, "SIDECAR_PATH", tmp_path / "none.json")
    features.update({k: {"enabled": False} for k in ("usage", "history", "archive", "learning")},
                    wire=False)
    assert _run_main(monkeypatch, "private words") == "private words"
    d = features.data_dir()
    assert not any(p.name not in (".lock",) for p in d.glob("*")) if d.exists() else True
    assert not history.last_record_path().exists()


def test_learned_stage_guarded_and_spans_protect_rules(xdg, monkeypatch, tmp_path):
    side = tmp_path / "metadata.json"
    side.write_text(json.dumps({"version": 3, "replacements": [
        {"from_text": "omashi linux", "to_text": "Omashi Linux", "category": "Replacement"}]}))
    seen = {}

    class FakeLearned:
        @staticmethod
        def apply(text, spans, user_phrases=None):
            seen["spans"] = spans
            seen["phrases"] = user_phrases
            return text.replace("tail", "TAIL"), ["l_1"]
    monkeypatch.setattr(cli_postprocess, "_learned_module", lambda: FakeLearned)
    text, changed_by, hits = cli_postprocess.process_detailed(
        "omashi linux tail", sidecar_path=side, config_path=tmp_path / "none.toml")
    assert text == "Omashi Linux TAIL"
    assert seen["spans"] == [(0, 12)] and "Omashi Linux" in seen["phrases"]
    assert changed_by == ["dictionary", "learned"] and hits == ["l_1"]
    monkeypatch.setattr(cli_postprocess, "_learned_module", lambda: None)
    assert cli_postprocess.process("a b", sidecar_path=side) == "a b"


# --- migration / state -------------------------------------------------------

def test_fresh_install_without_metadata_gets_wired_at_first_save(live_config):
    st = AppState.load(config.CONFIG_PATH, sidecar.SIDECAR_PATH)
    assert not st.config_dirty  # loading alone never dirties a fresh install
    st.save()
    text = live_config.read_text()
    assert "voxtype-tui-hook pre-recording" in text
    assert "voxtype-tui-postprocess" in text
    assert features.load()["managed_hooks"]


def test_upgrade_from_v3_runs_v4_and_wires(live_config):
    sidecar.SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    sidecar.SIDECAR_PATH.write_text(json.dumps({"version": 3, "vocabulary": [], "replacements": []}))
    st = AppState.load(config.CONFIG_PATH, sidecar.SIDECAR_PATH)
    assert st.sc.version == 4 == sidecar.SCHEMA_VERSION
    assert st.config_dirty and st.daemon_stale
    assert "insights-wire-hooks" in st.migrations_applied
    st.save()
    assert "voxtype-tui-hook pre-recording" in live_config.read_text()
    st2 = AppState.load(config.CONFIG_PATH, sidecar.SIDECAR_PATH)
    assert not st2.config_dirty  # idempotent


def test_alternate_config_is_never_rewired(xdg, tmp_path):
    cfg = tmp_path / "other.toml"
    cfg.write_text('[output]\nmode = "type"\n')
    st = AppState.load(cfg, tmp_path / "meta.json")
    assert "pre_recording_command" not in tomlkit.dumps(st.doc)


def test_failed_migration_does_not_advance_past_it(monkeypatch):
    calls = []

    def boom(doc, sc):
        calls.append("boom")
        raise RuntimeError("x")
    ms = [migrations.Migration(2, "a", "", lambda d, s: False, False),
          migrations.Migration(3, "b", "", boom, False),
          migrations.Migration(4, "c", "", lambda d, s: False, False)]
    monkeypatch.setattr(migrations, "MIGRATIONS", ms)
    sc = sidecar.Sidecar(version=1)
    res = migrations.run_pending(tomlkit.document(), sc)
    assert sc.version == 2 and res.new_version == 2
    ms[1] = migrations.Migration(3, "b", "", lambda d, s: False, False)
    res = migrations.run_pending(tomlkit.document(), sc)
    assert sc.version == sidecar.SCHEMA_VERSION


def test_pre_recording_command_is_a_dangerous_secret():
    assert ("output", "pre_recording_command") in sync.SECRET_PATHS
    assert ("output", "pre_recording_command") in sync.DANGEROUS_PATHS


def test_hook_entry_returns_fast_and_work_runs_detached(xdg):
    import subprocess
    import sys

    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=str(repo))
    t = time.perf_counter()
    r = subprocess.run([sys.executable, "-m", "voxtype_tui.hook_main", "pre-recording"],
                       env=env, capture_output=True, timeout=10, cwd=str(repo))
    elapsed = time.perf_counter() - t
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    for _ in range(100):
        if capture.rec_start_path().exists():
            break
        time.sleep(0.05)
    info = json.loads(capture.rec_start_path().read_text())
    assert abs(info["start"] - time.time()) < 10
    assert elapsed < 5
    r = subprocess.run([sys.executable, "-m", "voxtype_tui.hook_main", "bogus"],
                       env=env, capture_output=True, timeout=10, cwd=str(repo))
    assert r.returncode == 0


def test_enqueue_during_drain_never_waits_and_is_not_lost(xdg, monkeypatch):
    """maintain() renames the queue away under its flock (microseconds) and
    drains the renamed copy, so a dictation's enqueue never waits on a
    drain and is never truncated away (review r1 #1)."""
    history.enqueue(_rec(1))
    orig = history._update_health
    seen = {}

    def slow_health(*a, **k):
        t = time.perf_counter()
        seen["ok"] = history.enqueue(_rec(2))  # same process: would deadlock if locked
        seen["ms"] = (time.perf_counter() - t) * 1000
        orig(*a, **k)
    monkeypatch.setattr(history, "_update_health", slow_health)
    history.maintain()
    monkeypatch.setattr(history, "_update_health", orig)
    assert seen["ok"] is True and seen["ms"] < 50
    assert history.summary(30)["totals"]["dictations"] == 2


def test_crash_mid_drain_of_a_huge_batch_never_double_counts(xdg, monkeypatch):
    monkeypatch.setattr(history, "RECENT_IDS_KEEP", 5)
    for i in range(8):
        history.enqueue(_rec(i))
    batch = history.queue_path().read_bytes()
    history.maintain()
    history._draining_path(None).write_bytes(batch)  # crash before unlink
    history.maintain()
    assert history.summary(30)["totals"]["dictations"] == 8
    assert len(history.list_items(limit=50)["items"]) == 8


def test_get_never_returns_expired_records(xdg):
    history.enqueue(_rec(1, ts=time.time() - 40 * 86400))
    rid = json.loads(history.queue_path().read_text())["id"]
    assert history.get(rid) is None
    history.maintain()
    assert history.get(rid) is None


def test_delete_queued_item_keeps_count_but_drops_text(xdg):
    history.enqueue(_rec(1))
    rid = json.loads(history.queue_path().read_text())["id"]
    assert history.delete(rid) == 1
    q = history.queue_path()
    assert not q.exists() or "final" not in q.read_text()
    assert history.summary(30)["totals"]["dictations"] == 1
    assert history.list_items()["items"] == []
    assert history.get(rid) is None


def test_archive_only_without_text_keeps_no_text_anywhere(xdg, monkeypatch, tmp_path):
    """review r1 #2: archive on, save_text off, history/learning off."""
    monkeypatch.setattr(cli_postprocess, "SIDECAR_PATH", tmp_path / "none.json")
    features.update({"usage": {"enabled": False}, "history": {"enabled": False},
                     "learning": {"enabled": False},
                     "archive": {"enabled": True, "dir": str(tmp_path / "a"), "save_text": False}},
                    wire=False)
    assert _run_main(monkeypatch, "secret words") == "secret words"
    last = json.loads(history.last_record_path().read_text())
    assert "raw" not in last and "final" not in last and last["words"] == 2
    assert not history.queue_path().exists()


def _fake_recorder(tmp_path: Path) -> str:
    rec = tmp_path / "bin" / "pw-record"
    rec.parent.mkdir()
    rec.write_text("#!/bin/sh\nexec sleep 30\n")
    rec.chmod(0o755)
    return str(rec)


def test_recorder_is_stopped_when_handoff_fails(xdg, monkeypatch, tmp_path):
    """review r1 #3: a recorder must never outlive a failed setup."""
    import subprocess as sp

    features.update({"archive": {"enabled": True, "dir": str(tmp_path / "a")}}, wire=False)
    monkeypatch.setattr(hooks, "recorder_binary", lambda: _fake_recorder(tmp_path))
    monkeypatch.setattr(hooks, "resolve_source", lambda d: (None, "default source"))
    started = {}
    real_popen = sp.Popen

    def popen(*a, **k):
        p = real_popen(*a, **k)
        started["p"] = p
        return p
    monkeypatch.setattr(sp, "Popen", popen)
    real_write = features.atomic_write_text

    def failing_write(path, text):
        if Path(path).name == "rec-start.json":
            raise OSError("disk full")
        return real_write(path, text)
    monkeypatch.setattr(features, "atomic_write_text", failing_write)
    with pytest.raises(OSError):
        hooks.on_pre_recording()
    assert started["p"].poll() is not None  # stopped and reaped


def test_archive_claims_each_record_once_with_its_own_handoff(xdg, tmp_path):
    """review r1 #4/#5: finalizer A never touches B's handoff, and a second
    post-output for an old record never overwrites its archive entry."""
    import wave as wv

    from voxtype_tui import archive

    feats = features.update({"archive": {"enabled": True, "dir": str(tmp_path / "a")}}, wire=False)
    features.ensure_private_dir(features.runtime_dir() / "rec")
    recs = []
    for i in range(2):
        start = time.time() - 5 + i
        wav = features.runtime_dir() / "rec" / f"{i}.wav"
        with wv.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x01\x00" * 16000)
        rid = history.new_id()[:-4] + f"{i:04x}"
        features.atomic_write_text(capture.rec_used_path(rid), json.dumps(
            {"start": start, "wav": str(wav), "recorder_pid": 0, "recorder_started": start + 0.04}))
        recs.append({"id": rid, "ts": history.iso(time.time()), "started_at": history.iso(start),
                     "duration_s": 1.0, "raw": "r", "final": "f", "words": 1})
    entry = archive.finalize(recs[0], feats)
    assert entry["audio"] and capture.rec_used_path(recs[1]["id"]).exists()
    assert archive.finalize(recs[0], feats) is None  # claimed once
    meta = json.loads((tmp_path / "a" / entry["json"]).read_text())
    assert meta["audio"] == entry["audio"]
    assert archive.finalize(recs[1], feats)["audio"]
    lines = (tmp_path / "a" / "index.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert hooks._claim("x1") is True and hooks._claim("x1") is False


def test_unrecorded_copy_of_our_command_is_never_removed(xdg):
    """review r1 #7 (A3): modify/remove only the recorded string."""
    ours = hooks.OUR_COMMANDS[hooks.PRE_RECORDING]
    doc = _doc(f'[output]\npre_recording_command = "{ours}"\n')
    off = features.load()
    for k in ("usage", "history", "archive", "learning"):
        off[k]["enabled"] = False
    plan = hooks.reconcile(doc, off)
    assert doc["output"]["pre_recording_command"] == ours and not plan.changed
    plan = hooks.reconcile(doc, features.load())  # wanted: adopted, not edited
    assert hooks.PRE_RECORDING not in plan.wired
    assert plan.managed[hooks.PRE_RECORDING] == ours


def test_unwire_all_leaves_unrecorded_values(live_config):
    ours = hooks.OUR_COMMANDS[hooks.PRE_RECORDING]
    live_config.write_text(f'[output]\npre_recording_command = "{ours}"\n')
    assert hooks.unwire_all()["changed"] is False
    assert ours in live_config.read_text()


def test_apply_migrations_wires_a_fresh_install(live_config):
    from voxtype_tui import cli_migrate

    rc = cli_migrate.main(["--apply-migrations", "--config-path", str(live_config),
                           "--sidecar-path", str(sidecar.SIDECAR_PATH), "--no-restart", "--quiet"])
    assert rc == 0
    assert "voxtype-tui-hook pre-recording" in live_config.read_text()
    assert features.load()["managed_hooks"]


def test_resolve_source_matches_partial_device_names(monkeypatch):
    """review r1 #10: voxtype accepts partial device names; pw-record needs
    the node name."""
    import subprocess as sp

    out = ("50\talsa_output.pci-0000_00_1f.3.analog-stereo.monitor\tPipeWire\ts32le 2ch 48000Hz\tIDLE\n"
           "51\talsa_input.pci-0000_00_1f.3.analog-stereo\tPipeWire\ts32le 2ch 48000Hz\tIDLE\n")
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, out, ""))
    assert hooks.resolve_source("default") == (None, "default source")
    assert hooks.resolve_source("analog-stereo")[0] == "alsa_input.pci-0000_00_1f.3.analog-stereo"
    target, note = hooks.resolve_source("usb-mic")
    assert target is None and "not found" in note


def test_post_output_after_cancel_drops_scratch_audio_and_keeps_archive(xdg, tmp_path, monkeypatch):
    feats = features.update({"archive": {"enabled": True, "dir": str(tmp_path / "a")}}, wire=False)
    rec = {"id": history.new_id(), "ts": history.iso(time.time()), "final": "x", "words": 1}
    features.atomic_write_text(history.last_record_path(), json.dumps(rec))
    assert hooks._claim(rec["id"])  # already handled by its own post-output
    rt = features.ensure_private_dir(features.runtime_dir() / "rec")
    wav = rt / "1.wav"
    wav.write_bytes(b"RIFF")
    features.atomic_write_text(capture.rec_start_path(), json.dumps(
        {"start": time.time(), "wav": str(wav), "recorder_pid": 0}))
    called = []
    monkeypatch.setattr("voxtype_tui.archive.finalize", lambda *a, **k: called.append(a))
    hooks.on_post_output()
    assert not wav.exists() and not capture.rec_start_path().exists()
    assert called == []
    _ = feats
