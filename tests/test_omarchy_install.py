"""Bash-level tests for the Omarchy install/uninstall scripts.

Scripts assume voxtype-tui is already on PATH (AUR / pipx install), so each
test provides a sandbox PATH containing a stub `voxtype-tui` binary. The
"missing from PATH" test omits it to verify the exit-1 guard. `hyprctl` is
deliberately absent so the scripts' reload branch always no-ops.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "scripts" / "install-omarchy.sh"
UNINSTALL = REPO / "scripts" / "uninstall-omarchy.sh"


def _make_stub_voxtype_tui(dest: Path) -> Path:
    """Drop a trivial stub binary at dest/voxtype-tui so `command -v` finds
    it. The stub never actually runs during these tests."""
    dest.mkdir(parents=True, exist_ok=True)
    stub = dest / "voxtype-tui"
    stub.write_text("#!/usr/bin/env bash\necho stub\n")
    stub.chmod(0o755)
    return stub


@pytest.fixture
def sandbox_home(tmp_path: Path) -> Path:
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
    _make_stub_voxtype_tui(home / ".local" / "bin")
    return home


def run(
    script: Path,
    home: Path,
    *,
    include_stub: bool = True,
) -> subprocess.CompletedProcess:
    """Run a script with HOME pointed at the sandbox. PATH is a minimal list
    that optionally includes $HOME/.local/bin so the voxtype-tui stub is
    discoverable. Omit the stub by passing include_stub=False to exercise the
    missing-from-PATH guard."""
    path_parts = ["/usr/bin", "/bin"]
    if include_stub:
        path_parts.insert(0, str(home / ".local" / "bin"))
    env = {
        "HOME": str(home),
        "PATH": ":".join(path_parts),
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


def test_install_does_not_drop_a_wrapper(sandbox_home: Path) -> None:
    """AUR install provides /usr/bin/voxtype-tui directly; install-omarchy
    must NOT create a conda-activating wrapper."""
    # Remove the stub first, then re-create after install to see what was added
    stub_dir = sandbox_home / ".local" / "bin"
    stub = stub_dir / "voxtype-tui"
    before = stub.read_text()
    run(INSTALL, sandbox_home)
    # Stub wasn't overwritten with a conda wrapper
    after = stub.read_text()
    assert after == before
    assert "conda" not in after


def test_install_is_idempotent(sandbox_home: Path) -> None:
    """Running install twice must not duplicate managed lines."""
    run(INSTALL, sandbox_home)
    run(INSTALL, sandbox_home)

    bindings = (sandbox_home / ".config" / "hypr" / "bindings.conf").read_text()
    windows = (sandbox_home / ".config" / "hypr" / "windows.conf").read_text()

    assert bindings.count("voxtype-tui-managed") == 1
    assert windows.count("voxtype-tui-managed") == 1


# ---- install: guards ----

def test_install_bails_when_voxtype_tui_missing_from_path(sandbox_home: Path) -> None:
    """The script assumes voxtype-tui is installed system-wide — if it's
    not on PATH, refuse with exit 1 and a message pointing at AUR/pipx."""
    result = run(INSTALL, sandbox_home, include_stub=False)
    assert result.returncode == 1
    assert "voxtype-tui not found on PATH" in result.stderr
    assert "yay -S voxtype-tui" in result.stderr
    assert "pipx install voxtype-tui" in result.stderr


def test_install_bails_without_omarchy(tmp_path: Path) -> None:
    """If ~/.config/omarchy/ doesn't exist, install refuses with exit 1
    and a helpful message."""
    home = tmp_path
    (home / ".config").mkdir()  # no omarchy dir
    _make_stub_voxtype_tui(home / ".local" / "bin")
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
    assert "BIND_KEY" in result.stderr


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


def test_uninstall_does_not_remove_voxtype_tui_binary(sandbox_home: Path) -> None:
    """Uninstall should only touch Hyprland config — never the package
    binary (which might be system-owned, managed by pacman)."""
    run(INSTALL, sandbox_home)
    stub = sandbox_home / ".local" / "bin" / "voxtype-tui"
    assert stub.exists()
    run(UNINSTALL, sandbox_home)
    assert stub.exists()


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
