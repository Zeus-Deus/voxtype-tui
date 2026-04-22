"""Schema migrations for config.toml + sidecar.

Each migration is a pure function that mutates `(doc, sc)` in place and
returns True if it changed anything. Migrations are keyed by the schema
version they bring the sidecar to — running migration for target
version N assumes the sidecar is currently at version N-1 and produces
a sidecar at version N on success.

Three entry paths call :func:`run_pending`:

  1. **TUI startup** — `AppState.load()` runs pending migrations after
     reconcile; dirty flags get set so the next save persists.
  2. **Headless CLI** — `voxtype-tui --apply-migrations` loads state,
     runs pending migrations, saves, restarts the daemon if the config
     changed, exits. This is the AUR post-install path.
  3. **AUR pacman hook** — `contrib/voxtype-tui.install` prints a notice
     telling the user their config will migrate on next launch (or
     offers the headless CLI as a one-liner).

Migrations MUST be idempotent. Running them twice against an
already-migrated config must be a no-op. The version gate prevents the
common case of re-running, but defensive idempotence handles the edge
case where someone hand-edits the sidecar version back to 1 but leaves
the config already migrated.

The "touches_config" flag on each migration tells the caller whether a
daemon restart is required after this migration ran. Migrations that
only touch the sidecar (UI metadata) do not need a restart.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from tomlkit import TOMLDocument

from . import config, sidecar

_logger = logging.getLogger(__name__)


# Current schema version lives in sidecar.py (re-exported here for
# callers that already import from this module). Bumped in lockstep
# with any new entry in MIGRATIONS below.
from .sidecar import SCHEMA_VERSION  # noqa: E402


@dataclass(frozen=True)
class Migration:
    target_version: int
    name: str
    description: str
    # Returns True if the migration made a persistable change.
    apply: Callable[[TOMLDocument, sidecar.Sidecar], bool]
    # True when the migration writes to config.toml (and thus requires a
    # Voxtype daemon restart to take effect). False for sidecar-only
    # migrations — post-process CLI re-reads the sidecar on every
    # transcription, so no restart is needed for those.
    touches_config: bool


def _enable_postprocess(doc: TOMLDocument, sc: sidecar.Sidecar) -> bool:
    """v1 → v2: activate our Vexis-style post-processor for any install
    that already has dictionary rules.

    Before this migration, voxtype-tui relied on Voxtype's built-in
    ``[text].replacements`` pass (literal, case-insensitive). v0.2
    shipped a fuzzy post-processor CLI that runs after the daemon's
    text layer via ``[output.post_process]``. Existing installs need
    that hook wired automatically — otherwise the new fuzzy/Unicode
    behavior sits dormant until the user happens to change a setting.

    Idempotent: checks the current command before writing.
    Non-destructive: if the user has set their own post_process command
    (one of Voxtype's LLM-cleanup examples, say), we leave it alone.
    No-op when the sidecar has no replacement rules — nothing to run
    the hook against.
    """
    if not sc.replacements:
        return False
    pp = config.get_post_process(doc)
    existing = pp.get("command")
    if existing and existing != config.POSTPROCESS_COMMAND:
        return False
    want_timeout = config.POSTPROCESS_TIMEOUT_MS
    if existing == config.POSTPROCESS_COMMAND and pp.get("timeout_ms") == want_timeout:
        return False
    config.set_post_process(doc, config.POSTPROCESS_COMMAND, want_timeout)
    return True


MIGRATIONS: list[Migration] = [
    Migration(
        target_version=2,
        name="enable_postprocess",
        description="Enable Vexis-style fuzzy/Unicode post-processor",
        apply=_enable_postprocess,
        touches_config=True,
    ),
]


@dataclass
class MigrationResult:
    # Names of migrations that reported a change. Empty list = no-op run.
    applied: list[str]
    # True when at least one applied migration wrote to config.toml.
    touches_config: bool
    # Schema version AFTER the run. Equals SCHEMA_VERSION on success.
    new_version: int


def run_pending(doc: TOMLDocument, sc: sidecar.Sidecar) -> MigrationResult:
    """Apply every migration whose target_version > sidecar.version.

    Mutates `sc.version` to the new current version (even if no
    migration made visible changes — the version gate still advances so
    we don't re-scan next time). Mutates `doc` in place when a
    migration writes to config.

    Safe to call repeatedly: once sidecar.version == SCHEMA_VERSION
    this function short-circuits to an empty result.
    """
    applied: list[str] = []
    touches_config = False
    start = sc.version
    for migration in MIGRATIONS:
        if migration.target_version <= start:
            continue
        try:
            changed = migration.apply(doc, sc)
        except Exception as e:  # a broken migration must not brick the app
            _logger.warning(
                "migration %s (v%d) raised %s; skipping",
                migration.name, migration.target_version, e,
            )
            continue
        if changed:
            applied.append(migration.name)
            if migration.touches_config:
                touches_config = True
    # Advance the schema version regardless of whether any migration
    # had work to do — the version is the claim "every migration up to
    # N has been considered", not "every migration up to N made changes".
    sc.version = SCHEMA_VERSION
    return MigrationResult(
        applied=applied,
        touches_config=touches_config,
        new_version=SCHEMA_VERSION,
    )
