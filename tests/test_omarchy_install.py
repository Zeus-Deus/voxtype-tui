"""Bash-level tests for the Omarchy install/uninstall scripts.

Each test runs the script against a sandboxed `$HOME` so nothing touches the
real user config. `hyprctl` is absent in the test environment; the scripts
detect that via `command -v` and skip the reload silently."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "scripts" / "install-omarchy.sh"
UNINSTALL = REPO / "scripts" / "uninstall-omarchy.sh"


@pytest.fixture
def sandbox_home(tmp_path: Path) -> Path:
    """A fake $HOME with an Omarchy config layout minimal enough for the
    scripts to proceed past their sanity checks."""
    home = tmp_path
    (home / ".config" / "omarchy").mkdir(parents=True)
    hypr = home / ".config" / "hypr"
    hypr.mkdir(parents=True)
    (hypr / "bindings.conf").write_text(
        "# existing bindings.conf content\n"
        "bindd = SUPER, RETURN, Terminal, exec, terminal\n"
    )
    (hypr / "windows.conf").write_text(
        "# existing windows.conf content\n"
        "windowrule = float, class:^(some-app)$\n"
    )
    return home


def run(script: Path, home: Path) -> subprocess.CompletedProcess:
    """Run a script with HOME pointed at the sandbox. Strip PATH of any
    hyprctl so the scripts' reload branch always no-ops."""
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",  # no hyprctl
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


# ---- install: happy path ----

def test_install_writes_managed_lines(sandbox_home: Path) -> None:
    result = run(INSTALL, sandbox_home)
    assert result.returncode == 0, result.stderr

    bindings = (sandbox_home / ".config" / "hypr" / "bindings.conf").read_text()
    windows = (sandbox_home / ".config" / "hypr" / "windows.conf").read_text()

    assert "voxtype-tui-managed" in bindings
    assert "voxtype-tui-managed" in windows
    assert "SUPER CTRL ALT, X" in bindings
    assert "omarchy-launch-or-focus-tui voxtype-tui" in bindings
    assert "size 1100 750" in windows
    assert "match:initial_class org.omarchy.voxtype-tui" in windows


def test_install_creates_wrapper(sandbox_home: Path) -> None:
    result = run(INSTALL, sandbox_home)
    assert result.returncode == 0, result.stderr

    wrapper = sandbox_home / ".local" / "bin" / "voxtype-tui"
    assert wrapper.exists()
    assert os.access(wrapper, os.X_OK)
    content = wrapper.read_text()
    assert 'conda activate "voxtype-tui"' in content
    assert "python -m voxtype_tui" in content


def test_install_is_idempotent(sandbox_home: Path) -> None:
    """Running install twice must not duplicate managed lines."""
    run(INSTALL, sandbox_home)
    run(INSTALL, sandbox_home)

    bindings = (sandbox_home / ".config" / "hypr" / "bindings.conf").read_text()
    windows = (sandbox_home / ".config" / "hypr" / "windows.conf").read_text()

    assert bindings.count("voxtype-tui-managed") == 1
    assert windows.count("voxtype-tui-managed") == 1


# ---- install: guards ----

def test_install_bails_without_omarchy(tmp_path: Path) -> None:
    """If ~/.config/omarchy/ doesn't exist, install refuses with exit 1
    and a helpful message."""
    home = tmp_path
    (home / ".config").mkdir()  # no omarchy dir
    result = run(INSTALL, home)
    assert result.returncode == 1
    assert "Omarchy not detected" in result.stderr


def test_install_bails_on_conflicting_keybind(sandbox_home: Path) -> None:
    """If SUPER CTRL ALT, X is already bound to something, install bails
    with exit 2 and a clear message listing the conflict."""
    bindings = sandbox_home / ".config" / "hypr" / "bindings.conf"
    bindings.write_text(
        bindings.read_text()
        + "bindd = SUPER CTRL ALT, X, Some Other App, exec, other-app\n"
    )
    result = run(INSTALL, sandbox_home)
    assert result.returncode == 2
    assert "already exists on SUPER CTRL ALT, X" in result.stderr
    assert "edit BIND_KEY" in result.stderr.lower() or "BIND_KEY" in result.stderr


def test_install_doesnt_touch_configs_when_conflicting(sandbox_home: Path) -> None:
    bindings = sandbox_home / ".config" / "hypr" / "bindings.conf"
    original = (
        bindings.read_text()
        + "bindd = SUPER CTRL ALT, X, Some Other App, exec, other-app\n"
    )
    bindings.write_text(original)
    run(INSTALL, sandbox_home)
    assert bindings.read_text() == original


# ---- uninstall ----

def test_uninstall_removes_managed_lines(sandbox_home: Path) -> None:
    run(INSTALL, sandbox_home)
    bindings_before = (sandbox_home / ".config" / "hypr" / "bindings.conf").read_text()
    assert "voxtype-tui-managed" in bindings_before

    result = run(UNINSTALL, sandbox_home)
    assert result.returncode == 0, result.stderr

    bindings = (sandbox_home / ".config" / "hypr" / "bindings.conf").read_text()
    windows = (sandbox_home / ".config" / "hypr" / "windows.conf").read_text()
    assert "voxtype-tui-managed" not in bindings
    assert "voxtype-tui-managed" not in windows
    # The original existing lines should still be there
    assert "bindd = SUPER, RETURN, Terminal" in bindings
    assert "class:^(some-app)$" in windows


def test_uninstall_removes_wrapper(sandbox_home: Path) -> None:
    run(INSTALL, sandbox_home)
    wrapper = sandbox_home / ".local" / "bin" / "voxtype-tui"
    assert wrapper.exists()
    run(UNINSTALL, sandbox_home)
    assert not wrapper.exists()


def test_uninstall_is_noop_when_not_installed(sandbox_home: Path) -> None:
    """Running uninstall without a prior install shouldn't fail."""
    result = run(UNINSTALL, sandbox_home)
    assert result.returncode == 0, result.stderr


def test_install_uninstall_install_cycle(sandbox_home: Path) -> None:
    """A full install → uninstall → install cycle should leave the configs
    in the same state as a single install (no trailing cruft)."""
    run(INSTALL, sandbox_home)
    after_first = (sandbox_home / ".config" / "hypr" / "bindings.conf").read_text()
    run(UNINSTALL, sandbox_home)
    run(INSTALL, sandbox_home)
    after_cycle = (sandbox_home / ".config" / "hypr" / "bindings.conf").read_text()
    assert after_cycle == after_first
