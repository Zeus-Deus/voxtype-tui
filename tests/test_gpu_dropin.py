"""Unit tests for voxtype_tui.gpu — detection parsing + drop-in read/write.

Pure functions only; all filesystem effects go through tmp_path.
"""
from __future__ import annotations

from pathlib import Path

from voxtype_tui import gpu


# Real-world sample from `voxtype setup gpu --status` on a dual-GPU laptop.
SAMPLE_STATUS = """\
=== Voxtype Backend Status ===

Active backend: CPU (AVX-512)

GPUs detected:
  1. [Intel] Intel Corporation Raptor Lake-S UHD Graphics (rev 04)
  2. [NVIDIA] NVIDIA Corporation AD107M [GeForce RTX 4060 Max-Q / Mobile] (rev a1)

GPU selection: auto (first available)

Multiple GPUs detected. To select a specific GPU, set:
  VOXTYPE_VULKAN_DEVICE=nvidia   # Use NVIDIA GPU
"""


# ---- parse_detected_gpus ----

def test_parse_happy_path() -> None:
    gpus = gpu.parse_detected_gpus(SAMPLE_STATUS)
    assert gpus == [
        ("Intel — Raptor Lake-S UHD Graphics", "intel"),
        ("NVIDIA — AD107M [GeForce RTX 4060 Max-Q / Mobile]", "nvidia"),
    ]


def test_parse_empty_text() -> None:
    assert gpu.parse_detected_gpus("") == []


def test_parse_missing_block() -> None:
    text = "=== Voxtype Backend Status ===\n\nActive backend: CPU\n"
    assert gpu.parse_detected_gpus(text) == []


def test_parse_dedupes_by_vendor() -> None:
    text = (
        "GPUs detected:\n"
        "  1. [NVIDIA] NVIDIA Corporation AD107M (rev a1)\n"
        "  2. [NVIDIA] NVIDIA Corporation GA102 [RTX 3090] (rev a1)\n"
        "  3. [AMD] Advanced Micro Devices, Inc. Raphael (rev c1)\n"
    )
    gpus = gpu.parse_detected_gpus(text)
    # First NVIDIA kept, second dropped; AMD prefix stripped.
    assert gpus == [
        ("NVIDIA — AD107M", "nvidia"),
        ("AMD — Raphael", "amd"),
    ]


def test_parse_stops_at_end_of_numbered_block() -> None:
    gpus = gpu.parse_detected_gpus(SAMPLE_STATUS)
    # The "GPU selection: auto" line and trailer must not be parsed as GPUs.
    assert all(v in ("intel", "nvidia") for _, v in gpus)
    assert len(gpus) == 2


def test_parse_garbled_block_yields_empty() -> None:
    text = "GPUs detected:\nnothing here that looks like a list\n"
    assert gpu.parse_detected_gpus(text) == []


# ---- read_gpu_device ----

def test_read_missing_file(tmp_path: Path) -> None:
    assert gpu.read_gpu_device(tmp_path / "nope.conf") is None


def test_read_var_absent(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text("[Service]\nEnvironment=\"SOMETHING_ELSE=1\"\n")
    assert gpu.read_gpu_device(p) is None


def test_read_quoted(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text('[Service]\nEnvironment="VOXTYPE_VULKAN_DEVICE=nvidia"\n')
    assert gpu.read_gpu_device(p) == "nvidia"


def test_read_unquoted(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text("[Service]\nEnvironment=VOXTYPE_VULKAN_DEVICE=amd\n")
    assert gpu.read_gpu_device(p) == "amd"


# ---- write_gpu_device ----

def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    p = tmp_path / "systemd" / "user" / "voxtype.service.d" / "gpu.conf"
    gpu.write_gpu_device(p, "nvidia")
    assert p.exists()
    assert gpu.read_gpu_device(p) == "nvidia"
    assert "[Service]" in p.read_text()


def test_write_round_trip_set_change_clear(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"

    gpu.write_gpu_device(p, "nvidia")
    assert gpu.read_gpu_device(p) == "nvidia"

    # Change vendor — must not duplicate the Environment line.
    gpu.write_gpu_device(p, "intel")
    assert gpu.read_gpu_device(p) == "intel"
    env_lines = [
        l for l in p.read_text().splitlines()
        if "VOXTYPE_VULKAN_DEVICE" in l
    ]
    assert len(env_lines) == 1

    # Back to Auto — nothing but [Service] would remain, so file is gone.
    gpu.write_gpu_device(p, None)
    assert not p.exists()


def test_write_none_missing_file_is_noop(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    gpu.write_gpu_device(p, None)  # must not raise or create anything
    assert not p.exists()


def test_write_preserves_foreign_lines(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text(
        "# hand-added by the user\n"
        "[Service]\n"
        'Environment="FOO=bar"\n'
        'Environment="VOXTYPE_VULKAN_DEVICE=intel"\n'
    )
    gpu.write_gpu_device(p, "nvidia")
    text = p.read_text()
    assert "# hand-added by the user" in text
    assert 'Environment="FOO=bar"' in text
    assert gpu.read_gpu_device(p) == "nvidia"


def test_write_none_keeps_file_with_foreign_content(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text(
        "[Service]\n"
        'Environment="FOO=bar"\n'
        'Environment="VOXTYPE_VULKAN_DEVICE=intel"\n'
    )
    gpu.write_gpu_device(p, None)
    # Foreign Environment line means the file survives; only our line goes.
    assert p.exists()
    assert gpu.read_gpu_device(p) is None
    assert 'Environment="FOO=bar"' in p.read_text()


def test_write_none_keeps_file_with_comment(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text(
        "# keep me\n"
        "[Service]\n"
        'Environment="VOXTYPE_VULKAN_DEVICE=nvidia"\n'
    )
    gpu.write_gpu_device(p, None)
    assert p.exists()
    assert p.read_text().splitlines()[0] == "# keep me"
    assert gpu.read_gpu_device(p) is None


def test_write_none_deletes_when_only_service_and_blanks(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text(
        "\n"
        "[Service]\n"
        "\n"
        'Environment="VOXTYPE_VULKAN_DEVICE=nvidia"\n'
        "\n"
    )
    gpu.write_gpu_device(p, None)
    assert not p.exists()


def test_write_ensures_service_header(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text('Environment="FOO=bar"\n')  # no [Service] header
    gpu.write_gpu_device(p, "amd")
    lines = p.read_text().splitlines()
    assert "[Service]" in lines
    # Our line sits under the [Service] header.
    svc = lines.index("[Service]")
    assert any(
        "VOXTYPE_VULKAN_DEVICE=amd" in l for l in lines[svc + 1:]
    )
