"""'Learn everywhere' managed files: exact preview, byte-exact undo, user
edits preserved, nothing written without set_enabled()."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from voxtype_tui import learn_everywhere as LE


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("VOXTYPE_TUI_SYSTEMCTL", "0")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("chromium", "electron43", "obsidian"):
        (bindir / name).write_text("#!/bin/sh\n")
        (bindir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    c = tmp_path / "config"
    c.mkdir()
    return c


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
            and "voxtype-tui" not in p.parts}


def test_preview_writes_nothing_and_lists_installed_only(cfg):
    (cfg / "chromium-flags.conf").write_text("--ozone-platform=wayland\n")
    before = snapshot(cfg)
    pv = LE.preview()
    assert snapshot(cfg) == before
    paths = {Path(f["path"]).name: f for f in pv["files"]}
    assert set(paths) == {LE.ENV_FILE_NAME, "chromium-flags.conf", "electron43-flags.conf",
                          "user-flags.conf"}
    assert "brave-flags.conf" not in paths  # not installed
    assert paths["chromium-flags.conf"]["action"] == "append"
    assert "+--force-renderer-accessibility" in paths["chromium-flags.conf"]["diff"]
    assert pv["needs_relogin"] is True and pv["enable"] is True


def test_enable_then_disable_restores_bytes(cfg):
    (cfg / "chromium-flags.conf").write_text("--ozone-platform=wayland")  # no trailing newline
    (cfg / "obsidian").mkdir()
    (cfg / "obsidian" / "user-flags.conf").write_text("# mine\n-disable-gpu\n")
    before = snapshot(cfg)
    r = LE.set_enabled(True)
    assert r["ok"] and r["enabled"]
    assert (cfg / "environment.d" / LE.ENV_FILE_NAME).read_text().count("QT_ACCESSIBILITY=1") == 1
    assert "--force-renderer-accessibility" in (cfg / "chromium-flags.conf").read_text()
    assert LE.is_enabled()
    again = LE.set_enabled(True)  # idempotent
    assert (cfg / "chromium-flags.conf").read_text().count(LE.FLAG) == 1
    assert all(f["action"] == "none" for f in again["files"])
    LE.set_enabled(False)
    assert snapshot(cfg) == before
    assert not LE.is_enabled()


def test_disable_keeps_later_user_edits(cfg):
    (cfg / "chromium-flags.conf").write_text("--a\n")
    LE.set_enabled(True)
    p = cfg / "chromium-flags.conf"
    p.write_text(p.read_text() + "--user-added-later\n")
    env = cfg / "environment.d" / LE.ENV_FILE_NAME
    env.write_text(env.read_text() + "MY_VAR=1\n")
    LE.set_enabled(False)
    assert p.read_text() == "--a\n--user-added-later\n"
    assert env.read_text() == "MY_VAR=1\n"


def test_user_flag_already_present_is_left_alone(cfg):
    (cfg / "chromium-flags.conf").write_text("--force-renderer-accessibility\n")
    LE.set_enabled(True)
    LE.set_enabled(False)
    assert (cfg / "chromium-flags.conf").read_text() == "--force-renderer-accessibility\n"


def test_foreign_env_file_with_our_name_is_not_overwritten(cfg):
    d = cfg / "environment.d"
    d.mkdir()
    (d / LE.ENV_FILE_NAME).write_text("SOMETHING=else\n")
    LE.set_enabled(True)
    assert (d / LE.ENV_FILE_NAME).read_text() == "SOMETHING=else\n"
    LE.set_enabled(False)
    assert (d / LE.ENV_FILE_NAME).read_text() == "SOMETHING=else\n"


def test_electron_fallback_file_is_edited_not_shadowed(cfg):
    (cfg / "electron-flags.conf").write_text("--enable-wayland-ime\n")
    LE.set_enabled(True)
    assert not (cfg / "electron43-flags.conf").exists()
    assert LE.FLAG in (cfg / "electron-flags.conf").read_text()
    LE.set_enabled(False)
    assert (cfg / "electron-flags.conf").read_text() == "--enable-wayland-ime\n"


def test_created_files_are_removed_on_disable(cfg):
    LE.set_enabled(True)
    assert (cfg / "electron43-flags.conf").exists()
    LE.set_enabled(False)
    assert not (cfg / "electron43-flags.conf").exists()
    assert not (cfg / "environment.d" / LE.ENV_FILE_NAME).exists()


def test_r1_user_replaced_env_file_is_kept_on_disable(cfg):
    LE.set_enabled(True)
    env = cfg / "environment.d" / LE.ENV_FILE_NAME
    env.write_text("QT_ACCESSIBILITY=1\n")  # the user's own file now
    LE.set_enabled(False)
    assert env.read_text() == "QT_ACCESSIBILITY=1\n"


def test_r1_symlinked_flags_file_is_declined(cfg, tmp_path):
    real = tmp_path / "dotfiles-chromium.conf"
    real.write_text("--a\n")
    (cfg / "chromium-flags.conf").symlink_to(real)
    pv = {Path(f["path"]).name: f for f in LE.preview(True)["files"]}
    assert pv["chromium-flags.conf"]["action"] == "none"
    LE.set_enabled(True)
    LE.set_enabled(False)
    assert (cfg / "chromium-flags.conf").is_symlink() and real.read_text() == "--a\n"


def test_r1_concurrent_enables_keep_ownership(cfg):
    import threading
    ts = [threading.Thread(target=LE.set_enabled, args=(True,)) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(LE._load_state()["files"]) == 4  # env + chromium + electron43 + obsidian
    LE.set_enabled(False)
    assert not (cfg / "environment.d" / LE.ENV_FILE_NAME).exists()
    assert not (cfg / "electron43-flags.conf").exists()


def test_r2_disable_never_replaces_a_symlink(cfg, tmp_path):
    (cfg / "chromium-flags.conf").write_text("--a\n")
    LE.set_enabled(True)
    real = tmp_path / "dotfiles-chromium.conf"
    real.write_text((cfg / "chromium-flags.conf").read_text())
    (cfg / "chromium-flags.conf").unlink()
    (cfg / "chromium-flags.conf").symlink_to(real)
    LE.set_enabled(False)
    assert (cfg / "chromium-flags.conf").is_symlink()


def test_r2_session_env_restores_previous_values(cfg, monkeypatch):
    monkeypatch.setenv("VOXTYPE_TUI_SYSTEMCTL", "1")
    env = {"QT_ACCESSIBILITY": "0"}  # the user's own value before
    calls = []

    def fake_run(argv, **k):
        calls.append(argv)
        class R:
            returncode = 0
            stdout = ""
        r = R()
        if argv[:3] == ["systemctl", "--user", "show-environment"]:
            r.stdout = "".join(f"{a}={b}\n" for a, b in env.items())
        elif argv[:3] == ["systemctl", "--user", "set-environment"]:
            env.update(x.split("=", 1) for x in argv[3:])
        elif argv[:3] == ["systemctl", "--user", "unset-environment"]:
            for x in argv[3:]:
                env.pop(x, None)
        elif argv[:2] == ["busctl", "--user"]:
            r.stdout = "b true"
        return r
    monkeypatch.setattr(LE.subprocess, "run", fake_run)
    monkeypatch.setattr(LE, "_which", lambda n: True)
    monkeypatch.setattr(LE, "flag_files", lambda: [])
    LE.set_enabled(True)
    assert env["QT_ACCESSIBILITY"] == "1" and env["ACCESSIBILITY_ENABLED"] == "1"
    env["NO_AT_BRIDGE"] = "1"  # the user changed it meanwhile: not ours any more
    LE.set_enabled(False)
    assert env == {"QT_ACCESSIBILITY": "0", "NO_AT_BRIDGE": "1"}
    # disable without a prior enable never touches the session
    calls.clear()
    LE.set_enabled(False)
    assert not any("unset-environment" in c or "set-environment" in c for c in calls)


# ---------------------------------------------------------------- concurrent user edits

def _after_plan(monkeypatch, fn):
    """Run ``fn`` right after set_enabled planned its edits (a user or an
    editor saving the file in that window). Returns a function that turns
    the interference off again."""
    real = LE._plan

    def plan(enable):
        out = real(enable)
        fn()
        return out
    monkeypatch.setattr(LE, "_plan", plan)
    return lambda: monkeypatch.setattr(LE, "_plan", real)


def test_enable_never_overwrites_an_edit_made_meanwhile(cfg, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    stop = _after_plan(monkeypatch, lambda: p.write_text("--a\n--user-added-meanwhile\n"))
    r = LE.set_enabled(True)
    assert p.read_text() == "--a\n--user-added-meanwhile\n"
    assert r["ok"] is False and "changed" in r["error"] and r["conflict"] == str(p)
    assert str(p) not in LE._load_state()["files"]  # not recorded as ours
    stop()
    assert LE.set_enabled(True)["ok"]  # trying again works on the new content
    assert p.read_text() == "--a\n--user-added-meanwhile\n" + LE.MARKER + "\n" + LE.FLAG + "\n"


def test_enable_never_overwrites_a_file_created_meanwhile(cfg, monkeypatch):
    p = cfg / "electron43-flags.conf"
    _after_plan(monkeypatch, lambda: p.write_text("--mine\n"))
    r = LE.set_enabled(True)
    assert p.read_text() == "--mine\n" and r["ok"] is False


def test_disable_never_overwrites_an_edit_made_meanwhile(cfg, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    LE.set_enabled(True)
    edited = p.read_text() + "--user-added-meanwhile\n"
    stop = _after_plan(monkeypatch, lambda: p.write_text(edited))
    r = LE.set_enabled(False)
    assert p.read_text() == edited and r["ok"] is False
    assert str(p) in LE._load_state()["files"]  # still ours to clean up later
    stop()
    assert LE.set_enabled(False)["ok"]
    assert p.read_text() == "--a\n--user-added-meanwhile\n"


def test_disable_never_deletes_a_file_edited_meanwhile(cfg, monkeypatch):
    LE.set_enabled(True)
    p = cfg / "electron43-flags.conf"
    edited = p.read_text() + "--mine\n"
    _after_plan(monkeypatch, lambda: p.write_text(edited))
    LE.set_enabled(False)
    assert p.read_text() == edited


def test_a_file_turned_into_a_symlink_meanwhile_is_left_alone(cfg, tmp_path, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    real = tmp_path / "dotfiles-chromium.conf"

    def swap():
        real.write_text(p.read_text())
        p.unlink()
        p.symlink_to(real)
    _after_plan(monkeypatch, swap)
    r = LE.set_enabled(True)
    assert p.is_symlink() and real.read_text() == "--a\n" and r["ok"] is False


# ------------------------------------------------- saves between the last check and the write

def _after_check(monkeypatch, path, fn):
    """Run ``fn`` once, right after the final pre-write check of ``path``
    passed (an editor saving in the last instant)."""
    real = LE._expect
    fired = []

    def expect(p, item):
        real(p, item)
        if Path(p) == path and not fired:
            fired.append(1)
            fn()
    monkeypatch.setattr(LE, "_expect", expect)
    return lambda: monkeypatch.setattr(LE, "_expect", real)


def _editor_save(p, text):
    """What most editors do: write a new file, rename it over the old one."""
    tmp = p.with_name(p.name + ".editor-tmp")
    tmp.write_text(text)
    os.replace(tmp, p)


def test_enable_keeps_an_editor_save_made_after_the_last_check(cfg, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    stop = _after_check(monkeypatch, p, lambda: _editor_save(p, "--a\n--saved-late\n"))
    r = LE.set_enabled(True)
    assert p.read_text() == "--a\n--saved-late\n"
    assert r["ok"] is False and r["conflict"] == str(p)
    assert str(p) not in LE._load_state()["files"]
    stop()
    assert LE.set_enabled(True)["ok"]
    assert p.read_text() == "--a\n--saved-late\n" + LE.MARKER + "\n" + LE.FLAG + "\n"


def test_enable_keeps_an_in_place_save_made_after_the_last_check(cfg, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    _after_check(monkeypatch, p, lambda: p.write_text("--b\n"))
    r = LE.set_enabled(True)
    assert p.read_text() == "--b\n" and r["ok"] is False


def test_enable_never_replaces_a_symlink_made_after_the_last_check(cfg, tmp_path, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    real = tmp_path / "dotfiles-chromium.conf"

    def swap():
        real.write_text("--a\n")
        p.unlink()
        p.symlink_to(real)
    _after_check(monkeypatch, p, swap)
    r = LE.set_enabled(True)
    assert p.is_symlink() and os.readlink(p) == str(real)
    assert real.read_text() == "--a\n" and r["ok"] is False


def test_disable_keeps_an_editor_save_made_after_the_last_check(cfg, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    assert LE.set_enabled(True)["ok"]
    edited = p.read_text() + "--saved-late\n"
    stop = _after_check(monkeypatch, p, lambda: _editor_save(p, edited))
    r = LE.set_enabled(False)
    assert p.read_text() == edited and r["ok"] is False and r["conflict"] == str(p)
    assert str(p) in LE._load_state()["files"]
    assert sorted(x.name for x in cfg.iterdir() if x.name.startswith(".chromium")) == []
    stop()
    assert LE.set_enabled(False)["ok"]
    assert p.read_text() == "--a\n--saved-late\n"


def test_disable_never_replaces_a_symlink_made_after_the_last_check(cfg, tmp_path, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    assert LE.set_enabled(True)["ok"]
    real = tmp_path / "dotfiles-chromium.conf"
    ours = p.read_text()

    def swap():
        real.write_text(ours)
        p.unlink()
        p.symlink_to(real)
    _after_check(monkeypatch, p, swap)
    r = LE.set_enabled(False)
    assert p.is_symlink() and os.readlink(p) == str(real)
    assert real.read_text() == ours and r["ok"] is False


# ------------------------------------------- saves inside the write itself (exact moments)

def _at(monkeypatch, stage, path, fn):
    fired = []

    def hook(st, p):
        if st == stage and Path(p) == path and not fired:
            fired.append(1)
            fn()
    monkeypatch.setattr(LE, "_race_hook", hook)
    return fired


def _leftovers(d):
    return sorted(x.name for x in d.iterdir() if ".voxtype-tui" in x.name)


def test_enable_append_keeps_an_in_place_write_racing_it(cfg, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")

    def other_writer():
        with open(p, "a") as f:
            f.write("--other\n")
    assert _at(monkeypatch, "append", p, other_writer) is not None
    r = LE.set_enabled(True)
    assert r["ok"] is True
    assert p.read_text() == "--a\n--other\n" + LE.MARKER + "\n" + LE.FLAG + "\n"


def test_enable_append_reports_a_rename_save_racing_it(cfg, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    fired = _at(monkeypatch, "append", p, lambda: _editor_save(p, "--saved\n"))
    r = LE.set_enabled(True)
    assert fired and p.read_text() == "--saved\n"
    assert r["ok"] is False and r["conflict"] == str(p)
    assert str(p) not in LE._load_state()["files"]


def test_enable_append_never_follows_a_symlink_racing_it(cfg, tmp_path, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    real = tmp_path / "dotfiles.conf"

    def swap():
        real.write_text("--a\n")
        p.unlink()
        p.symlink_to(real)
    fired = _at(monkeypatch, "append", p, swap)
    r = LE.set_enabled(True)
    assert fired and r["ok"] is False
    assert p.is_symlink() and real.read_text() == "--a\n"


def test_disable_claim_puts_back_a_file_saved_just_before_the_move(cfg, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    assert LE.set_enabled(True)["ok"]
    edited = p.read_text() + "--late\n"
    fired = _at(monkeypatch, "claim", p, lambda: _editor_save(p, edited))
    r = LE.set_enabled(False)
    assert fired and r["ok"] is False and r["conflict"] == str(p) and "kept" not in r
    assert p.read_text() == edited and not p.is_symlink()
    assert _leftovers(cfg) == []
    assert str(p) in LE._load_state()["files"]


def test_disable_claim_puts_back_a_symlink_made_just_before_the_move(cfg, tmp_path, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    assert LE.set_enabled(True)["ok"]
    ours = p.read_text()
    real = tmp_path / "dotfiles.conf"

    def swap():
        real.write_text(ours)
        p.unlink()
        p.symlink_to(real)
    fired = _at(monkeypatch, "claim", p, swap)
    r = LE.set_enabled(False)
    assert fired and r["ok"] is False
    assert p.is_symlink() and os.readlink(p) == str(real) and real.read_text() == ours
    assert _leftovers(cfg) == []


def test_disable_publish_keeps_a_file_recreated_meanwhile(cfg, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    assert LE.set_enabled(True)["ok"]
    ours = p.read_text()
    fired = _at(monkeypatch, "publish", p, lambda: p.write_text("--brand-new\n"))
    r = LE.set_enabled(False)
    assert fired and r["ok"] is False and r["conflict"] == str(p)
    assert p.read_text() == "--brand-new\n"            # the newer file wins
    assert Path(r["kept"]).read_text() == ours         # and nothing is lost
    assert Path(r["kept"]).name == "chromium-flags.conf.voxtype-tui-kept"
    assert [x for x in _leftovers(cfg) if not x.endswith("-kept")] == []


def test_disable_edit_keeps_the_file_mode(cfg):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    p.chmod(0o600)
    assert LE.set_enabled(True)["ok"] and LE.set_enabled(False)["ok"]
    assert p.read_text() == "--a\n" and (p.stat().st_mode & 0o777) == 0o600
    assert _leftovers(cfg) == []


def test_disable_remove_keeps_a_created_file_edited_just_before_the_move(cfg, monkeypatch):
    LE.set_enabled(True)
    p = cfg / "electron43-flags.conf"
    edited = p.read_text() + "--mine\n"
    fired = _at(monkeypatch, "claim", p, lambda: _editor_save(p, edited))
    r = LE.set_enabled(False)
    assert fired and r["ok"] is False and p.read_text() == edited
    assert _leftovers(cfg) == []


# ------------------------------- writes through a descriptor opened before disabling

def _open_writer(p):
    """A settings writer that opened the file before Learn everywhere was
    turned off and appends through that same descriptor later."""
    f = open(p, "a")

    def write():
        f.write("--other\n")
        f.flush()
    return f, write


def test_disable_edit_keeps_a_write_through_an_open_descriptor_before_publish(cfg, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    assert LE.set_enabled(True)["ok"]
    ours = p.read_text()
    f, write = _open_writer(p)
    try:
        fired = _at(monkeypatch, "publish", p, write)
        r = LE.set_enabled(False)
    finally:
        f.close()
    assert fired and r["ok"] is False and r["conflict"] == str(p)
    assert p.read_text() == ours + "--other\n"   # the changed original is back in place
    assert _leftovers(cfg) == []
    assert str(p) in LE._load_state()["files"]


def test_disable_edit_keeps_a_write_through_an_open_descriptor_after_publish(cfg, monkeypatch):
    p = cfg / "chromium-flags.conf"
    p.write_text("--a\n")
    assert LE.set_enabled(True)["ok"]
    ours = p.read_text()
    f, write = _open_writer(p)
    try:
        fired = _at(monkeypatch, "discard", p, write)
        r = LE.set_enabled(False)
    finally:
        f.close()
    assert fired and r["ok"] is False and r["conflict"] == str(p)
    assert Path(r["kept"]).read_text() == ours + "--other\n"   # nothing is lost
    assert Path(r["kept"]).name == "chromium-flags.conf.voxtype-tui-kept"
    assert str(p) in r["error"] and r["kept"] in r["error"]
    assert [x for x in _leftovers(cfg) if not x.endswith("-kept")] == []


def test_disable_remove_keeps_a_write_through_an_open_descriptor(cfg, monkeypatch):
    LE.set_enabled(True)
    p = cfg / "electron43-flags.conf"
    ours = p.read_text()
    f, write = _open_writer(p)
    try:
        fired = _at(monkeypatch, "discard", p, write)
        r = LE.set_enabled(False)
    finally:
        f.close()
    assert fired and r["ok"] is False and r["conflict"] == str(p)
    assert p.read_text() == ours + "--other\n"
    assert _leftovers(cfg) == []
