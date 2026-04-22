"""Headless migration runner.

Invoked as ``voxtype-tui --apply-migrations`` (the app.py main() hands
off when it sees the flag). Designed to be safely called from three
places:

  1. The AUR pacman post-install hook (`contrib/voxtype-tui.install`).
  2. A manual one-liner for power users after upgrading.
  3. Test code.

Behavior:

- Loads state from the standard paths (or user-overridden via
  ``--config-path`` / ``--sidecar-path`` for tests).
- Runs every pending migration (implicit — ``AppState.load`` already
  invokes :func:`migrations.run_pending`).
- If migrations applied config changes, saves the migrated config +
  sidecar and restarts the voxtype daemon (unless ``--no-restart``).
- Prints one human-readable line per decision so AUR post-install
  output is actually useful to users.
- Exits 0 on success (including the no-op "already current" case),
  non-zero on save/restart failure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import config, sidecar, voxtype_cli
from .state import AppState


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="voxtype-tui --apply-migrations",
        description="Run pending voxtype-tui schema migrations headlessly.",
    )
    p.add_argument("--apply-migrations", action="store_true",
                   help="(required marker; consumed by app.main)")
    p.add_argument("--config-path", type=Path, default=config.CONFIG_PATH,
                   help=f"Path to voxtype config.toml (default: {config.CONFIG_PATH})")
    p.add_argument("--sidecar-path", type=Path, default=sidecar.SIDECAR_PATH,
                   help=f"Path to voxtype-tui metadata.json (default: {sidecar.SIDECAR_PATH})")
    p.add_argument("--no-restart", action="store_true",
                   help="Skip the daemon restart even if config changed.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress informational output (errors still printed).")
    return p.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv if argv is not None else sys.argv[1:])
    out = (lambda *a, **k: None) if args.quiet else print

    # The voxtype config file must exist before we can migrate anything.
    # Fresh voxtype-tui installs where the user hasn't run voxtype yet
    # is a valid skip — nothing to do until they configure the daemon.
    if not args.config_path.exists():
        out(f"==> voxtype-tui: no voxtype config at {args.config_path}; nothing to migrate.")
        return 0

    try:
        state = AppState.load(args.config_path, args.sidecar_path)
    except Exception as e:
        print(f"error: failed to load voxtype config: {e}", file=sys.stderr)
        return 1

    applied = state.migrations_applied
    if not applied and not state.config_dirty:
        out("==> voxtype-tui: schema already current, no migrations to apply.")
        return 0

    if applied:
        out(f"==> voxtype-tui: applied migrations: {', '.join(applied)}")

    try:
        state.save()
    except config.ValidationError as e:
        print(f"error: voxtype rejected migrated config: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: could not save migrated config: {e}", file=sys.stderr)
        return 1

    out("==> voxtype-tui: migrated config saved.")

    if args.no_restart:
        out("==> voxtype-tui: skipping daemon restart (--no-restart).")
        return 0

    if not state.daemon_stale:
        return 0

    if not voxtype_cli.is_daemon_active():
        out("==> voxtype-tui: voxtype daemon not running; no restart needed.")
        return 0

    ok, msg = voxtype_cli.restart_daemon()
    if ok:
        out("==> voxtype-tui: voxtype daemon restarted.")
        return 0
    print(f"error: failed to restart voxtype: {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
