"""Headless facade for the plugin bridge / any UI (see docs/insights.md).

One function per bridge op; each returns the op's body without ``ok``.
Raises ``ValueError`` for bad input and :class:`Unavailable` when a
feature cannot run (the bridge maps it to ``{ok:false, error, unavailable}``).
Stdlib only at import time.
"""
from __future__ import annotations

from pathlib import Path

from . import features as F
from . import history

FEATURES = ("usage", "history", "archive", "learning", "everywhere")


class Unavailable(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _reasons(feats: dict, config_path: Path | None = None) -> dict[str, str]:
    from . import config, hooks

    path = config_path or config.CONFIG_PATH
    try:
        import tomllib

        with path.open("rb") as f:
            doc = tomllib.load(f)
    except (OSError, ValueError):
        doc = {}
    return hooks.availability(doc, feats)


def get_features(config_path: Path | None = None) -> dict:
    feats = F.load()
    reasons = _reasons(feats, config_path)
    return {
        "features": F.public(feats),
        "available": {f: f not in reasons for f in FEATURES},
        "reasons": reasons,
    }


def set_features(patch: dict, config_path: Path | None = None) -> dict:
    from . import hooks

    F.update(patch, wire=False)
    wiring = hooks.sync_config(config_path=config_path)
    out = get_features(config_path)
    out["restart_needed"] = bool(wiring["changed"])
    out["config_changed"] = bool(wiring["changed"])
    return out


def usage_summary(days: int = 365, config_path: Path | None = None) -> dict:
    feats = F.load()
    out = history.summary(days, feats=feats)
    reasons = _reasons(feats, config_path)
    unavailable = [{"feature": f, "reason": r} for f, r in reasons.items()
                   if f in ("usage", "history", "archive")]
    from . import hooks

    try:
        import tomllib

        from . import config

        with (config_path or config.CONFIG_PATH).open("rb") as f:
            doc = tomllib.load(f)
        cur = hooks._get(doc, hooks.PRE_RECORDING)
        if cur and str(cur) not in (hooks.OUR_COMMANDS[hooks.PRE_RECORDING],
                                    feats.get(F.MANAGED_HOOKS_KEY, {}).get(hooks.PRE_RECORDING)):
            unavailable.append({"feature": "duration",
                                "reason": "your own pre_recording_command is set"})
    except (OSError, ValueError):
        pass
    out["health"]["unavailable"] = unavailable
    return out


def history_list(before: str | None = None, limit: int = 20) -> dict:
    if before is not None and not isinstance(before, str):
        raise ValueError("before must be a record id")
    return history.list_items(before=before, limit=limit)


def history_delete(record_id: str) -> dict:
    if not isinstance(record_id, str) or not record_id:
        raise ValueError("id is required")
    return {"removed": history.delete(record_id)}


def history_clear() -> dict:
    return {"removed": history.clear()}
