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
    error = None
    try:
        wiring = hooks.sync_config(config_path=config_path)
    except Exception as e:  # e.g. voxtype rejected the config: switches are saved,
        # wiring is retried on the next save/set; say so instead of failing.
        history.note_failure("features.set", f"hook wiring: {e}")
        wiring = {"changed": False}
        error = f"could not update voxtype's config: {str(e).splitlines()[0][:120]}"
    out = get_features(config_path)
    out["restart_needed"] = bool(wiring["changed"])
    out["config_changed"] = bool(wiring["changed"])
    if error:
        out["wiring_error"] = error
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
    # Right after an upgrade nothing has saved the config yet: the switches
    # are on but voxtype does not call us. Say so instead of showing zero
    # usage. Re-saving any switch connects it.
    pending = _pending_wiring(feats, config_path)
    for feature in pending:
        unavailable.append({"feature": feature, "reason": "not connected to Voxtype yet"})
    out["health"]["unavailable"] = unavailable
    out["health"]["pending_wiring"] = pending
    return out


def _pending_wiring(feats: dict, config_path: Path | None = None) -> list[str]:
    """Features whose hook voxtype's config does not have yet: "capture"
    (no post-process tap, so nothing is counted), "duration" (no
    pre-recording hook) or "learning" (no post-output hook). A dry run of
    :func:`hooks.reconcile` on a private copy; nothing is written."""
    from . import config, hooks

    path = config_path or config.CONFIG_PATH
    try:
        if not path.exists():
            return []
        doc = config.load(path)
        tapped = config.is_our_post_process_command(config.get_post_process(doc).get("command"))
        wired = hooks.reconcile(doc, feats).wired
    except Exception:  # unreadable config: availability reports it elsewhere
        return []
    out = []
    if "output.post_process.command" in wired and not tapped:
        out.append("capture")
    if hooks.PRE_RECORDING in wired:
        out.append("duration")
    # post_output also drains the queue for usage/history, but those keep
    # working without it (reads and the post-process drain the queue), so
    # only the archive and the learning watcher report it as pending.
    if hooks.POST_OUTPUT in wired and (
            feats["archive"]["enabled"]
            or (feats["learning"]["enabled"] and hooks._module_exists("voxtype_tui.learn_watch"))):
        out.append("learning")
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
