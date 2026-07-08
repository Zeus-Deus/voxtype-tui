"""GPU device selector — manage the systemd *user* drop-in that pins
`VOXTYPE_VULKAN_DEVICE` on multi-GPU machines.

This is strictly per-device hardware config. It lives in a systemd drop-in
(`~/.config/systemd/user/voxtype.service.d/gpu.conf`), NEVER in
`config.toml`, and is deliberately excluded from `sync.json` — a machine's
GPU vendor doesn't travel with the user's vocabulary/dictionary. See
CLAUDE.md non-goals.

Selecting a GPU writes TWO Environment lines into the drop-in:

  Environment="VOXTYPE_VULKAN_DEVICE=<vendor>"
  Environment="VK_LOADER_DRIVERS_SELECT=<pattern>"

The first is voxtype's own knob, but in daemon mode it's applied too late —
the Vulkan loader has already enumerated every ICD during model preload by
the time voxtype's code runs, so ggml sees all GPUs and silently picks
device 0 (the iGPU on a hybrid laptop). `VK_LOADER_DRIVERS_SELECT` is read
by the Vulkan LOADER itself at `vkCreateInstance`, before any voxtype code
runs, so it actually filters the ICD list. Verified fix: 127s → 1.78s for
an 87.5s clip on an Intel+NVIDIA hybrid laptop.

Selecting *Auto* removes both lines and deletes the file if nothing but a
bare `[Service]` header remains. Both take effect after
`systemctl --user daemon-reload` + a daemon restart (the "GPU selection:"
line in `voxtype setup gpu --status` reflects the CLI process env, not the
daemon's — the drop-in file is the source of truth).

Everything here is pure / path-parameterized so tests drive it against a
`tmp_path` without ever touching the real `~/.config`. The two functions
that shell out (`daemon_reload`) are module-level names so the UI layer can
be tested by monkeypatching them.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# Not expanduser()'d here — every function takes an explicit path so tests
# stay off the real filesystem. Call sites do `DROPIN_PATH.expanduser()`.
DROPIN_PATH = Path("~/.config/systemd/user/voxtype.service.d/gpu.conf")

# Recognized vendor keys accepted by VOXTYPE_VULKAN_DEVICE.
VENDORS: tuple[str, ...] = ("nvidia", "amd", "intel")

# The second env var: consumed by the Vulkan LOADER itself (before any
# voxtype code runs), so it's what actually pins the GPU in daemon mode.
LOADER_ENV_VAR = "VK_LOADER_DRIVERS_SELECT"

# Glob patterns matching Vulkan ICD manifest filenames in
# /usr/share/vulkan/icd.d/ (comma-separated alternatives), keyed by the
# same vendor strings VOXTYPE_VULKAN_DEVICE accepts. Mirrors voxtype's own
# internal table.
VENDOR_LOADER_PATTERNS: dict[str, str] = {
    "nvidia": "nvidia*",
    "amd": "*radeon*,*amd*",
    "intel": "*intel*",
}

# One numbered "GPUs detected:" row, e.g.
#   "  2. [NVIDIA] NVIDIA Corporation AD107M [GeForce RTX 4060 …] (rev a1)"
# The vendor tag is the FIRST bracket; the label may contain later brackets.
_GPU_LINE_RE = re.compile(
    r"^\s*\d+\.\s*\[(?P<vendor>[A-Za-z]+)\]\s*(?P<label>.*?)\s*$"
)

# Trailing "(rev a1)" / "(rev 04)" noise.
_REV_RE = re.compile(r"\s*\(rev\s+[^)]*\)\s*$", re.IGNORECASE)

# Our Environment line, quoted or unquoted, single or double quotes:
#   Environment="VOXTYPE_VULKAN_DEVICE=nvidia"
#   Environment=VOXTYPE_VULKAN_DEVICE=nvidia
_ENV_RE = re.compile(
    r"""^\s*Environment\s*=\s*
        ["']?VOXTYPE_VULKAN_DEVICE=(?P<value>[^"'\s]*)["']?\s*$""",
    re.VERBOSE,
)

# Same shape, for the loader-side var. Values are glob patterns
# (`nvidia*`, `*radeon*,*amd*`) — no quotes or whitespace, so the same
# "stop at quote/space" value class works.
_LOADER_ENV_RE = re.compile(
    r"""^\s*Environment\s*=\s*
        ["']?VK_LOADER_DRIVERS_SELECT=(?P<value>[^"'\s]*)["']?\s*$""",
    re.VERBOSE,
)

_SERVICE_HEADER_RE = re.compile(r"^\s*\[Service\]\s*$")


def _reverse_map_pattern(value: str) -> str | None:
    """Reverse-map a VK_LOADER_DRIVERS_SELECT value back to a vendor key.
    Exact match against the known VENDOR_LOADER_PATTERNS values only —
    an unrecognized pattern (hand-edited or from a future vendor) yields
    None rather than guessing."""
    for vendor, pattern in VENDOR_LOADER_PATTERNS.items():
        if pattern == value:
            return vendor
    return None


# --- detection parsing ------------------------------------------------------


def _clean_gpu_label(raw: str, vendor_tag: str) -> str:
    """Trim a raw lspci-style description down to something scannable:
    drop the trailing "(rev …)" and the redundant vendor-corporation
    prefix ("NVIDIA Corporation", "Intel Corporation", "Advanced Micro
    Devices, Inc."). Falls back to the vendor tag if nothing is left."""
    s = _REV_RE.sub("", raw).strip()
    for prefix in (
        f"{vendor_tag} Corporation",
        "Advanced Micro Devices, Inc.",
    ):
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):].strip()
            break
    return s or vendor_tag


def parse_detected_gpus(status_text: str) -> list[tuple[str, str]]:
    """Parse the "GPUs detected:" block of `voxtype setup gpu --status`.

    Returns `(human_label, vendor)` pairs, e.g.
    `("NVIDIA — AD107M [GeForce RTX 4060 Max-Q / Mobile]", "nvidia")`.
    Vendor comes from the `[NVIDIA]`/`[AMD]`/`[Intel]` bracket tag,
    lowercased. Deduped by vendor (the env var is vendor-keyed; keep the
    first occurrence). A missing or garbled block yields `[]`.
    """
    lines = status_text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("gpus detected"):
            start = i + 1
            break
    if start is None:
        return []

    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lines[start:]:
        m = _GPU_LINE_RE.match(line)
        if m is None:
            # Tolerate blank lines before the list starts; once numbered
            # rows begin, the first non-matching line ends the block.
            if not results and line.strip() == "":
                continue
            break
        vendor = m.group("vendor").lower()
        if vendor in seen:
            continue
        seen.add(vendor)
        tag = m.group("vendor")
        label = f"{tag} — {_clean_gpu_label(m.group('label'), tag)}"
        results.append((label, vendor))
    return results


# --- drop-in read/write -----------------------------------------------------


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except (FileNotFoundError, OSError):
        return []


def read_gpu_device(path: Path) -> str | None:
    """Return the configured GPU vendor from an existing drop-in, or None
    if the file is missing or neither var is present.

    Priority: `VOXTYPE_VULKAN_DEVICE` first (matches historical/CLI
    behavior). If absent, fall back to reverse-mapping
    `VK_LOADER_DRIVERS_SELECT` through VENDOR_LOADER_PATTERNS — an
    unrecognized pattern yields None rather than a guess."""
    voxtype_value: str | None = None
    loader_value: str | None = None
    for line in _read_lines(path):
        if voxtype_value is None:
            m = _ENV_RE.match(line)
            if m is not None:
                voxtype_value = m.group("value").strip() or None
        if loader_value is None:
            m2 = _LOADER_ENV_RE.match(line)
            if m2 is not None:
                loader_value = m2.group("value").strip() or None
    if voxtype_value is not None:
        return voxtype_value
    if loader_value is not None:
        return _reverse_map_pattern(loader_value)
    return None


def write_gpu_device(path: Path, vendor: str | None) -> None:
    """Set (or clear) the GPU vendor in the drop-in.

    - `vendor` in {"nvidia","amd","intel"}: create parent dirs and
      write/replace BOTH our managed lines — `VOXTYPE_VULKAN_DEVICE` and
      `VK_LOADER_DRIVERS_SELECT` — preserving every other line the user
      may have hand-added (foreign `Environment=` lines, comments). A
      `[Service]` header is ensured above the lines. Replacing an
      old-format file (only the VOXTYPE_VULKAN_DEVICE line, from v0.1.7)
      upgrades it in place to both lines, no duplicates.
    - `vendor is None` (Auto): remove BOTH managed lines. If nothing but
      blank lines and a bare `[Service]` header remains, delete the file
      entirely (the directory is left alone). Missing file → no-op.

    Writes are atomic (tempfile in the same dir + os.replace), matching
    `config.safe_save` / `sync._atomic_write_text`.
    """
    if vendor is None:
        _remove_gpu_device(path)
    else:
        _set_gpu_device(path, vendor)


def _set_gpu_device(path: Path, vendor: str) -> None:
    our_line = f'Environment="VOXTYPE_VULKAN_DEVICE={vendor}"'
    # An unrecognized vendor (e.g. a secondary display controller reported
    # as `[ASPEED]`/`[Matrox]`, or a VM's `[VirtIO]`/`[VMware]` GPU) has no
    # entry in VENDOR_LOADER_PATTERNS. Writing an empty
    # `VK_LOADER_DRIVERS_SELECT=""` would be a live, degenerate glob that
    # the Vulkan loader could read as "match nothing" — filtering out every
    # ICD and breaking GPU acceleration entirely, which is worse than the
    # pre-existing (harmless, voxtype-ignored) VOXTYPE_VULKAN_DEVICE-only
    # no-op. So: only emit the loader line for a recognized vendor; for an
    # unrecognized one, write just the VOXTYPE_VULKAN_DEVICE line (and drop
    # any stale loader line already present) — same safe fallback as before
    # this feature existed.
    pattern = VENDOR_LOADER_PATTERNS.get(vendor)
    loader_line = f'Environment="{LOADER_ENV_VAR}={pattern}"' if pattern else None
    existing = _read_lines(path)

    out: list[str] = []
    replaced = False
    for line in existing:
        if _ENV_RE.match(line) or _LOADER_ENV_RE.match(line):
            if not replaced:
                out.append(our_line)
                if loader_line is not None:
                    out.append(loader_line)
                replaced = True
            # drop any duplicate/old-format managed lines
            continue
        out.append(line)

    if not any(_SERVICE_HEADER_RE.match(l) for l in out):
        out.insert(0, "[Service]")

    if not replaced:
        new_lines = [our_line] if loader_line is None else [our_line, loader_line]
        idx = next(
            (i for i, l in enumerate(out) if _SERVICE_HEADER_RE.match(l)),
            None,
        )
        if idx is None:
            out.extend(new_lines)
        else:
            out[idx + 1:idx + 1] = new_lines

    _atomic_write(path, "\n".join(out).strip("\n") + "\n")


def _remove_gpu_device(path: Path) -> None:
    if not path.exists():
        return
    out = [
        l for l in _read_lines(path)
        if not _ENV_RE.match(l) and not _LOADER_ENV_RE.match(l)
    ]
    # Anything that isn't a blank line or a [Service] header is real,
    # user-authored content — keep the file if any survives.
    meaningful = [
        l for l in out if l.strip() and not _SERVICE_HEADER_RE.match(l)
    ]
    if not meaningful:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic_write(path, "\n".join(out).strip("\n") + "\n")


def dropin_needs_heal(path: Path) -> str | None:
    """Detect a drop-in that needs the two-line self-heal (see module
    docstring) without writing anything.

    Returns the configured vendor when the file exists, a vendor is
    configured via either managed line, and the pair of lines isn't in
    canonical state (e.g. a v0.1.7 file with only VOXTYPE_VULKAN_DEVICE
    and no loader line, a stale/mismatched loader pattern, or a loader
    line with no companion VOXTYPE_VULKAN_DEVICE line). Returns None for:
    a missing file, no vendor configured, or an already-canonical file.
    """
    if not path.exists():
        return None

    voxtype_value: str | None = None
    loader_value: str | None = None
    for line in _read_lines(path):
        if voxtype_value is None:
            m = _ENV_RE.match(line)
            if m is not None:
                voxtype_value = m.group("value").strip() or None
        if loader_value is None:
            m2 = _LOADER_ENV_RE.match(line)
            if m2 is not None:
                loader_value = m2.group("value").strip() or None

    vendor = voxtype_value or (
        _reverse_map_pattern(loader_value) if loader_value else None
    )
    if vendor is None:
        return None

    expected_loader = VENDOR_LOADER_PATTERNS.get(vendor)
    canonical = voxtype_value == vendor and loader_value == expected_loader
    return None if canonical else vendor


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


# --- daemon reload (shell-out; module-level so the UI can be mocked) --------


def daemon_reload(timeout: float = 5.0) -> tuple[bool, str]:
    """Run `systemctl --user daemon-reload` so systemd picks up the new
    drop-in. This does NOT restart the unit (that stays deferred to the
    TUI's normal stale-pill / restart-on-exit flow). Returns
    `(ran, message)`; skips gracefully when systemctl is absent."""
    if shutil.which("systemctl") is None:
        return False, "systemctl not found — skipped daemon-reload"
    try:
        result = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "daemon-reload timed out"
    except OSError as e:
        return False, f"daemon-reload could not run: {e}"
    if result.returncode == 0:
        return True, "ok"
    return False, (result.stderr or result.stdout).strip() or "daemon-reload failed"
