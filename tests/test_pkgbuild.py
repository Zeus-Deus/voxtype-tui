"""Smoke-verify that the PKGBUILD parses cleanly and exposes the metadata we
expect. `makepkg --printsrcinfo` is used instead of a real build so tests
don't need a clean chroot or actually download the source tarball."""
from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PKGBUILD = REPO / "PKGBUILD"
SRCINFO = REPO / ".SRCINFO"
PYPROJECT = REPO / "pyproject.toml"


def _pyproject_version() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


@pytest.fixture
def srcinfo_text() -> str:
    if shutil.which("makepkg") is None:
        pytest.skip("makepkg not installed")
    result = subprocess.run(
        ["makepkg", "--printsrcinfo", "-D", str(REPO)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_pkgbuild_parses() -> None:
    assert PKGBUILD.exists()


def test_srcinfo_is_committed() -> None:
    """.SRCINFO must be committed so AUR can read it without invoking
    makepkg on every fetch."""
    assert SRCINFO.exists()


def test_srcinfo_matches_pkgbuild(srcinfo_text: str) -> None:
    """Regenerating .SRCINFO from PKGBUILD must match the committed copy,
    else someone forgot to run `makepkg --printsrcinfo > .SRCINFO` after
    editing PKGBUILD."""
    committed = SRCINFO.read_text()
    assert srcinfo_text == committed, (
        "Committed .SRCINFO is stale. Run:\n"
        "    makepkg --printsrcinfo -D . > .SRCINFO"
    )


def test_srcinfo_declares_expected_metadata(srcinfo_text: str) -> None:
    required = [
        "pkgbase = voxtype-tui",
        "pkgname = voxtype-tui",
        f"pkgver = {_pyproject_version()}",
        "arch = any",
        "license = MIT",
        "depends = python",
        "depends = python-textual",
        "depends = python-tomlkit",
        "depends = voxtype-bin",
        "makedepends = python-build",
        "makedepends = python-installer",
    ]
    for line in required:
        assert line in srcinfo_text, f"missing {line!r} from .SRCINFO"


def test_pkgbuild_references_desktop_and_license() -> None:
    """The package() stage must install the files we ship alongside the
    Python wheel (icon integration + LICENSE for the AUR packaging policy)."""
    text = PKGBUILD.read_text()
    assert "contrib/voxtype-tui.desktop" in text
    assert "/usr/share/applications/voxtype-tui.desktop" in text
    assert "install -Dm644 LICENSE" in text
    assert "/usr/share/licenses/$pkgname/LICENSE" in text


def test_pkgbuild_uses_python_build_flow() -> None:
    """Modern Arch Python packaging: python -m build + python -m installer,
    no setup.py invocation."""
    text = PKGBUILD.read_text()
    assert "python -m build --wheel --no-isolation" in text
    assert "python -m installer --destdir=" in text


def test_desktop_file_basic_shape() -> None:
    desktop = REPO / "contrib" / "voxtype-tui.desktop"
    assert desktop.exists()
    text = desktop.read_text()
    assert "[Desktop Entry]" in text
    assert "Exec=omarchy-launch-or-focus-tui voxtype-tui" in text
    assert "StartupWMClass=org.omarchy.voxtype-tui" in text
    assert "Terminal=false" in text
