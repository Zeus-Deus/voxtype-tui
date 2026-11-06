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
