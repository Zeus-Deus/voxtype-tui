#!/usr/bin/env python
"""Round-trip canary for voxtype-tui.

Verifies tomlkit can make the v1 first-time edits we care about against the
real user config at ~/.config/voxtype/config.toml:

  1. Add `initial_prompt` under [whisper] (fresh key in an existing section).
  2. Create [text].replacements (the [text] section only exists as a comment
     block in the default template, so this materializes a fresh section).

Without destroying comments or reordering existing sections.

Cosmetic rewrites are treated as PASS:
  - inline-table `replacements = { ... }` becoming dotted/multiline form
  - trailing whitespace tweaks around section boundaries

Run: python scripts/roundtrip_check.py
Exit 0 = pass, 1 = fail (details printed), 2 = setup error.
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

import tomlkit


CONFIG_PATH = Path.home() / ".config" / "voxtype" / "config.toml"


def real_sections(text: str) -> list[str]:
    """Section headers from actual TOML (skip commented-out headers)."""
    out: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("[") and not s.startswith("#"):
            out.append(s)
    return out


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found", file=sys.stderr)
        return 2

    original = CONFIG_PATH.read_text()
    doc = tomlkit.parse(original)

    doc["whisper"]["initial_prompt"] = "Claude Code, Voxtype, Omarchy"

    text_tbl = tomlkit.table()
    reps = tomlkit.inline_table()
    reps["vox type"] = "voxtype"
    reps["cloud code"] = "Claude Code"
    text_tbl["replacements"] = reps
    doc["text"] = text_tbl

    rewritten = tomlkit.dumps(doc)

    failures: list[str] = []

    # Every comment line from the original must still exist somewhere in the
    # rewritten output. Presence beats line-for-line match because tomlkit may
    # re-anchor blank lines around sections.
    original_comments = [
        ln.rstrip() for ln in original.splitlines() if ln.lstrip().startswith("#")
    ]
    rewritten_lines = {ln.rstrip() for ln in rewritten.splitlines()}
    missing = [c for c in original_comments if c not in rewritten_lines]
    if missing:
        failures.append(
            f"{len(missing)} comment line(s) disappeared:\n    "
            + "\n    ".join(missing[:10])
            + ("" if len(missing) <= 10 else f"\n    ... and {len(missing) - 10} more")
        )

    # Intended additions must be present. Loose checks — don't pin on formatting.
    if "initial_prompt" not in rewritten:
        failures.append("initial_prompt key not found in output")
    if '"Claude Code, Voxtype, Omarchy"' not in rewritten:
        failures.append("initial_prompt value not found in output")
    if '"vox type"' not in rewritten or '"voxtype"' not in rewritten:
        failures.append('replacement {"vox type" -> "voxtype"} not found')
    if '"cloud code"' not in rewritten or '"Claude Code"' not in rewritten:
        failures.append('replacement {"cloud code" -> "Claude Code"} not found')

    # Pre-existing section order must not flip. New sections (e.g. [text],
    # [text.replacements]) may appear anywhere; that's fine.
    orig_secs = real_sections(original)
    new_secs = real_sections(rewritten)
    filtered = [s for s in new_secs if s in orig_secs]
    if filtered != orig_secs:
        failures.append(
            "existing section order changed:\n"
            f"    before: {orig_secs}\n"
            f"    after:  {filtered}"
        )

    # --- report ---
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            rewritten.splitlines(keepends=True),
            fromfile="config.toml (original)",
            tofile="config.toml (after round-trip)",
            n=2,
        )
    )
    print("=== diff ===")
    print(diff if diff else "(no diff)")
    print()

    if failures:
        print("=== FAIL ===")
        for f in failures:
            print(f"- {f}")
        return 1

    print("=== PASS ===")
    print(
        "Comments preserved, existing section order unchanged, "
        "intended additions present."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
