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


# ---- VK_LOADER_DRIVERS_SELECT (two-line write/read) ------------------------


def test_write_sets_both_lines(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    gpu.write_gpu_device(p, "nvidia")
    text = p.read_text()
    assert 'Environment="VOXTYPE_VULKAN_DEVICE=nvidia"' in text
    assert 'Environment="VK_LOADER_DRIVERS_SELECT=nvidia*"' in text
    assert gpu.read_gpu_device(p) == "nvidia"


def test_write_none_removes_both_lines(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    gpu.write_gpu_device(p, "amd")
    assert p.exists()
    gpu.write_gpu_device(p, None)
    # Nothing but [Service] would remain -> file deleted.
    assert not p.exists()


def test_write_upgrades_old_format_file_in_place(tmp_path: Path) -> None:
    """A v0.1.7 drop-in has only VOXTYPE_VULKAN_DEVICE. Re-writing must add
    the loader line without duplicating anything."""
    p = tmp_path / "gpu.conf"
    p.write_text('[Service]\nEnvironment="VOXTYPE_VULKAN_DEVICE=nvidia"\n')
    gpu.write_gpu_device(p, "nvidia")
    lines = p.read_text().splitlines()
    voxtype_lines = [l for l in lines if "VOXTYPE_VULKAN_DEVICE" in l]
    loader_lines = [l for l in lines if "VK_LOADER_DRIVERS_SELECT" in l]
    assert voxtype_lines == ['Environment="VOXTYPE_VULKAN_DEVICE=nvidia"']
    assert loader_lines == ['Environment="VK_LOADER_DRIVERS_SELECT=nvidia*"']


def test_write_changing_vendor_replaces_both_lines_no_duplicates(
    tmp_path: Path,
) -> None:
    p = tmp_path / "gpu.conf"
    gpu.write_gpu_device(p, "nvidia")
    gpu.write_gpu_device(p, "amd")
    lines = p.read_text().splitlines()
    assert [l for l in lines if "VOXTYPE_VULKAN_DEVICE" in l] == [
        'Environment="VOXTYPE_VULKAN_DEVICE=amd"'
    ]
    assert [l for l in lines if "VK_LOADER_DRIVERS_SELECT" in l] == [
        'Environment="VK_LOADER_DRIVERS_SELECT=*radeon*,*amd*"'
    ]
    assert gpu.read_gpu_device(p) == "amd"


def test_write_preserves_foreign_lines_two_line_format(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text(
        "# hand-added by the user\n"
        "[Service]\n"
        'Environment="FOO=bar"\n'
        'Environment="VOXTYPE_VULKAN_DEVICE=intel"\n'
        'Environment="VK_LOADER_DRIVERS_SELECT=*intel*"\n'
    )
    gpu.write_gpu_device(p, "nvidia")
    text = p.read_text()
    assert "# hand-added by the user" in text
    assert 'Environment="FOO=bar"' in text
    assert gpu.read_gpu_device(p) == "nvidia"
    assert text.count("VOXTYPE_VULKAN_DEVICE") == 1
    assert text.count("VK_LOADER_DRIVERS_SELECT") == 1


def test_write_none_keeps_file_with_foreign_content_two_line(
    tmp_path: Path,
) -> None:
    p = tmp_path / "gpu.conf"
    gpu.write_gpu_device(p, "intel")
    with p.open("a") as f:
        f.write('Environment="FOO=bar"\n')
    gpu.write_gpu_device(p, None)
    assert p.exists()
    assert gpu.read_gpu_device(p) is None
    text = p.read_text()
    assert 'Environment="FOO=bar"' in text
    assert "VOXTYPE_VULKAN_DEVICE" not in text
    assert "VK_LOADER_DRIVERS_SELECT" not in text


# ---- write_gpu_device: unrecognized vendor safe fallback -------------------


def test_write_unrecognized_vendor_omits_loader_line(tmp_path: Path) -> None:
    """A vendor with no entry in VENDOR_LOADER_PATTERNS (e.g. a secondary
    display controller like `aspeed`, or a VM GPU like `virtio`) must never
    get an empty/broken VK_LOADER_DRIVERS_SELECT line — that would filter
    out every Vulkan ICD instead of none. Only the VOXTYPE_VULKAN_DEVICE
    line (voxtype's own, harmless-if-unrecognized knob) is written."""
    p = tmp_path / "gpu.conf"
    gpu.write_gpu_device(p, "aspeed")
    text = p.read_text()
    assert 'Environment="VOXTYPE_VULKAN_DEVICE=aspeed"' in text
    assert "VK_LOADER_DRIVERS_SELECT" not in text
    assert gpu.read_gpu_device(p) == "aspeed"


def test_write_unrecognized_vendor_drops_stale_loader_line(
    tmp_path: Path,
) -> None:
    """Switching from a recognized vendor to an unrecognized one must clean
    up the now-stale loader line rather than leaving a mismatched pattern
    behind."""
    p = tmp_path / "gpu.conf"
    gpu.write_gpu_device(p, "nvidia")
    gpu.write_gpu_device(p, "aspeed")
    lines = p.read_text().splitlines()
    assert [l for l in lines if "VOXTYPE_VULKAN_DEVICE" in l] == [
        'Environment="VOXTYPE_VULKAN_DEVICE=aspeed"'
    ]
    assert [l for l in lines if "VK_LOADER_DRIVERS_SELECT" in l] == []


def test_dropin_needs_heal_unrecognized_vendor_is_canonical(
    tmp_path: Path,
) -> None:
    """An unrecognized vendor with no loader line is the correct/canonical
    state for that vendor (there's no pattern to add), so it must not be
    flagged for healing."""
    p = tmp_path / "gpu.conf"
    gpu.write_gpu_device(p, "aspeed")
    assert gpu.dropin_needs_heal(p) is None


# ---- read_gpu_device: loader-pattern fallback ------------------------------


def test_read_fallback_via_loader_pattern_nvidia(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text('[Service]\nEnvironment="VK_LOADER_DRIVERS_SELECT=nvidia*"\n')
    assert gpu.read_gpu_device(p) == "nvidia"


def test_read_fallback_via_loader_pattern_amd(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text(
        '[Service]\nEnvironment="VK_LOADER_DRIVERS_SELECT=*radeon*,*amd*"\n'
    )
    assert gpu.read_gpu_device(p) == "amd"


def test_read_fallback_via_loader_pattern_intel(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text('[Service]\nEnvironment="VK_LOADER_DRIVERS_SELECT=*intel*"\n')
    assert gpu.read_gpu_device(p) == "intel"


def test_read_voxtype_line_takes_priority_over_loader(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text(
        "[Service]\n"
        'Environment="VOXTYPE_VULKAN_DEVICE=amd"\n'
        'Environment="VK_LOADER_DRIVERS_SELECT=nvidia*"\n'
    )
    assert gpu.read_gpu_device(p) == "amd"


def test_read_unknown_loader_pattern_yields_none(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text(
        '[Service]\nEnvironment="VK_LOADER_DRIVERS_SELECT=*mali*"\n'
    )
    assert gpu.read_gpu_device(p) is None


# ---- dropin_needs_heal ------------------------------------------------------


def test_dropin_needs_heal_missing_file(tmp_path: Path) -> None:
    assert gpu.dropin_needs_heal(tmp_path / "nope.conf") is None


def test_dropin_needs_heal_no_vendor_configured(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text('[Service]\nEnvironment="SOMETHING_ELSE=1"\n')
    assert gpu.dropin_needs_heal(p) is None


def test_dropin_needs_heal_canonical_file_is_none(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    gpu.write_gpu_device(p, "nvidia")
    assert gpu.dropin_needs_heal(p) is None


def test_dropin_needs_heal_old_format_v017_file(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text('[Service]\nEnvironment="VOXTYPE_VULKAN_DEVICE=nvidia"\n')
    assert gpu.dropin_needs_heal(p) == "nvidia"


def test_dropin_needs_heal_stale_mismatched_pattern(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    p.write_text(
        "[Service]\n"
        'Environment="VOXTYPE_VULKAN_DEVICE=amd"\n'
        'Environment="VK_LOADER_DRIVERS_SELECT=nvidia*"\n'
    )
    assert gpu.dropin_needs_heal(p) == "amd"


def test_dropin_needs_heal_loader_only_file(tmp_path: Path) -> None:
    """Loader line present but no companion VOXTYPE line — still needs
    healing (heal adds the VOXTYPE_VULKAN_DEVICE line back)."""
    p = tmp_path / "gpu.conf"
    p.write_text('[Service]\nEnvironment="VK_LOADER_DRIVERS_SELECT=*intel*"\n')
    assert gpu.dropin_needs_heal(p) == "intel"


def test_dropin_needs_heal_is_read_only(tmp_path: Path) -> None:
    p = tmp_path / "gpu.conf"
    original = '[Service]\nEnvironment="VOXTYPE_VULKAN_DEVICE=nvidia"\n'
    p.write_text(original)
    gpu.dropin_needs_heal(p)
    assert p.read_text() == original
