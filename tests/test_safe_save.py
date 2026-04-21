"""Tests for safe_save: pre-save validation + first-write backup + atomicity."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import tomlkit

from voxtype_tui import config
from .conftest import FIXTURES, validates_with_voxtype


@pytest.fixture
def stock_in_tmp(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    shutil.copy(FIXTURES / "stock.toml", p)
    return p


def test_safe_save_valid_doc_succeeds(stock_in_tmp: Path) -> None:
    doc = config.load(stock_in_tmp)
    config.set_initial_prompt(doc, "Hello, World")
    config.safe_save(doc, stock_in_tmp)
    reloaded = config.load(stock_in_tmp)
    assert config.get_initial_prompt(reloaded) == "Hello, World"
    ok, _ = validates_with_voxtype(stock_in_tmp)
    assert ok


def test_safe_save_rejects_invalid_doc(stock_in_tmp: Path) -> None:
    original_bytes = stock_in_tmp.read_bytes()
    doc = config.load(stock_in_tmp)
    doc["engine"] = "not-a-real-engine-variant"

    with pytest.raises(config.ValidationError):
        config.safe_save(doc, stock_in_tmp)

    # Original file must be untouched — both contents AND the file itself
    # (no race where it briefly disappeared).
    assert stock_in_tmp.read_bytes() == original_bytes


def test_safe_save_cleans_up_tmp_on_rejection(stock_in_tmp: Path) -> None:
    doc = config.load(stock_in_tmp)
    doc["engine"] = "nonsense"
    with pytest.raises(config.ValidationError):
        config.safe_save(doc, stock_in_tmp)
    # No leftover *.tmp files in the directory
    leftover = list(stock_in_tmp.parent.glob("config.toml.*.tmp"))
    assert leftover == [], f"leftover tmp files: {leftover}"


def test_first_save_creates_backup(stock_in_tmp: Path) -> None:
    bak = stock_in_tmp.parent / "config.toml.voxtype-tui-bak"
    assert not bak.exists()
    original_bytes = stock_in_tmp.read_bytes()

    doc = config.load(stock_in_tmp)
    config.set_initial_prompt(doc, "Foo")
    config.safe_save(doc, stock_in_tmp)

    assert bak.exists(), "first save should create a backup"
    assert bak.read_bytes() == original_bytes, "backup should be pre-save state"


def test_subsequent_saves_do_not_overwrite_backup(stock_in_tmp: Path) -> None:
    bak = stock_in_tmp.parent / "config.toml.voxtype-tui-bak"

    doc = config.load(stock_in_tmp)
    config.set_initial_prompt(doc, "Foo")
    config.safe_save(doc, stock_in_tmp)
    first_bak_bytes = bak.read_bytes()

    doc2 = config.load(stock_in_tmp)
    config.set_initial_prompt(doc2, "Bar, Baz")
    config.safe_save(doc2, stock_in_tmp)

    assert bak.read_bytes() == first_bak_bytes, (
        "backup must be written only on first save, not subsequent"
    )


def test_safe_save_disable_validation(stock_in_tmp: Path) -> None:
    """With validate=False, we skip the voxtype check entirely. Useful for
    tests or for when the user insists on saving something weird."""
    doc = config.load(stock_in_tmp)
    # This would normally be rejected
    doc["engine"] = "bogus-engine"
    config.safe_save(doc, stock_in_tmp, validate=False)
    # And the file now contains the bogus value
    reloaded = tomlkit.parse(stock_in_tmp.read_text())
    assert str(reloaded["engine"]) == "bogus-engine"


def test_safe_save_disable_backup(stock_in_tmp: Path) -> None:
    doc = config.load(stock_in_tmp)
    config.set_initial_prompt(doc, "Foo")
    config.safe_save(doc, stock_in_tmp, backup=False)
    bak = stock_in_tmp.parent / "config.toml.voxtype-tui-bak"
    assert not bak.exists()


def test_safe_save_new_file_no_backup(tmp_path: Path) -> None:
    """Saving to a path that doesn't yet exist shouldn't create a backup of
    nothing."""
    new_path = tmp_path / "fresh.toml"
    doc = config.load(FIXTURES / "stock.toml")
    config.safe_save(doc, new_path)
    assert new_path.exists()
    assert not (tmp_path / "fresh.toml.voxtype-tui-bak").exists()


def test_validate_with_voxtype_on_valid_file() -> None:
    ok, _ = config.validate_with_voxtype(FIXTURES / "stock.toml")
    assert ok


def test_validate_with_voxtype_on_malformed_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("this is [not valid =\ntoml at all")
    ok, msg = config.validate_with_voxtype(bad)
    assert not ok
    assert msg  # should have an error message to show the user
