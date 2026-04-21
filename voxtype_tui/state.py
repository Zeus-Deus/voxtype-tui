"""Application state: the loaded config doc + sidecar, plus dirty flags.

Centralizes mutations so the app doesn't have to remember to flip dirty bits
at every call site. Keeps us from paying to re-serialize on every keystroke —
the baseline (`loaded_dump`) is captured at load time and only updated on save.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
from tomlkit import TOMLDocument

from . import config, sidecar, sync

_logger = logging.getLogger(__name__)


def _toml_equals(a: object, b: object) -> bool:
    """Compare a tomlkit-wrapped value with a Python primitive. tomlkit bool
    isn't a bool subclass so direct == can be surprising — fall back to string
    form for a defensive last check."""
    try:
        if a == b:
            return True
    except Exception:
        pass
    try:
        return str(a) == str(b)
    except Exception:
        return False


@dataclass
class AppState:
    doc: TOMLDocument
    sc: sidecar.Sidecar
    config_path: Path
    sidecar_path: Path
    loaded_dump: str
    config_dirty: bool = False
    sidecar_dirty: bool = False
    # True after a save touched a restart-sensitive field and the user
    # hasn't yet restarted the daemon. Cleared by a successful
    # restart_daemon_async. Persists across the current session only —
    # restarting the TUI itself re-derives from scratch on load.
    daemon_stale: bool = False
    reconcile_warnings: list[str] = field(default_factory=list)
    # Populated at load time. Banners in the app shell read this to
    # decide what to surface (conflict files, applied-from device,
    # missing model). Defaults to an empty result; same shape no matter
    # whether the feature applied, skipped, or hit an error.
    sync_reconcile: "sync.SyncReconcileResult" = field(
        default_factory=lambda: sync.SyncReconcileResult()
    )

    @property
    def dirty(self) -> bool:
        return self.config_dirty or self.sidecar_dirty

    @classmethod
    def load(cls, config_path: Path, sidecar_path: Path) -> "AppState":
        doc = config.load(config_path)
        raw_sc = sidecar.load(sidecar_path)

        initial = config.get_initial_prompt(doc)
        cfg_reps = config.get_replacements(doc)

        vocab, w1 = sidecar.reconcile_vocab(raw_sc.vocabulary, initial)
        reps, w2 = sidecar.reconcile_replacements(raw_sc.replacements, cfg_reps)

        sc = sidecar.Sidecar(vocabulary=vocab, replacements=reps, version=raw_sc.version)

        # If reconcile changed the sidecar, mark it dirty so the next save
        # persists the corrections.
        raw_cats = [(r.from_text, r.category) for r in raw_sc.replacements]
        new_cats = [(r.from_text, r.category) for r in sc.replacements]
        raw_vocab = [v.phrase for v in raw_sc.vocabulary]
        new_vocab = [v.phrase for v in sc.vocabulary]
        sidecar_dirty = raw_cats != new_cats or raw_vocab != new_vocab

        # Startup sync reconcile. Mutates `doc` + `sc` in place when a
        # newer sync.json is applied (and writes them back to disk
        # atomically so loaded state matches disk). The `loaded_dump`
        # snapshot below therefore captures post-apply doc, which is
        # correct — dirty flags stay at their current derived values
        # because apply + immediate persist leaves nothing pending.
        sync_result = sync.reconcile_sync_on_startup(
            doc, sc,
            config_path=config_path,
            sidecar_path=sidecar_path,
        )

        return cls(
            doc=doc,
            sc=sc,
            config_path=config_path,
            sidecar_path=sidecar_path,
            loaded_dump=tomlkit.dumps(doc),
            sidecar_dirty=sidecar_dirty,
            reconcile_warnings=w1 + w2,
            sync_reconcile=sync_result,
        )

    # --- mutations (all set config_dirty and/or sidecar_dirty) ---

    def set_vocabulary(self, phrases: list[str]) -> None:
        new_vocab: list[sidecar.VocabEntry] = []
        by_phrase = {v.phrase: v for v in self.sc.vocabulary}
        for p in phrases:
            new_vocab.append(by_phrase.get(p, sidecar.VocabEntry(phrase=p)))
        self.sc.vocabulary = new_vocab
        config.set_initial_prompt(self.doc, sidecar.build_initial_prompt(new_vocab) or None)
        self.config_dirty = True
        self.sidecar_dirty = True

    def add_vocab(self, phrase: str) -> bool:
        phrase = phrase.strip()
        if not phrase or any(v.phrase == phrase for v in self.sc.vocabulary):
            return False
        self.set_vocabulary([v.phrase for v in self.sc.vocabulary] + [phrase])
        return True

    def remove_vocab(self, phrase: str) -> bool:
        if not any(v.phrase == phrase for v in self.sc.vocabulary):
            return False
        self.set_vocabulary(
            [v.phrase for v in self.sc.vocabulary if v.phrase != phrase]
        )
        return True

    def upsert_replacement(self, from_text: str, to_text: str, category: str) -> None:
        """Insert or update a replacement. Fires `config_dirty` only if the
        on-disk (from, to) actually changed — category-only edits leave the
        config untouched and flip only `sidecar_dirty`."""
        if category not in sidecar.CATEGORIES:
            category = sidecar.DEFAULT_CATEGORY
        reps = config.get_replacements(self.doc)
        if reps.get(from_text) != to_text:
            reps[from_text] = to_text
            config.set_replacements(self.doc, reps)
            self.config_dirty = True
        for r in self.sc.replacements:
            if r.from_text == from_text:
                if r.category != category:
                    r.category = category
                    self.sidecar_dirty = True
                break
        else:
            self.sc.replacements.append(
                sidecar.ReplacementEntry(from_text=from_text, category=category)
            )
            self.sidecar_dirty = True

    def set_replacement_category(self, from_text: str, category: str) -> bool:
        """Sidecar-only category change. Returns True if category actually
        changed, False if it was already that category or the entry doesn't
        exist. Never touches config.toml."""
        if category not in sidecar.CATEGORIES:
            return False
        for r in self.sc.replacements:
            if r.from_text == from_text:
                if r.category == category:
                    return False
                r.category = category
                self.sidecar_dirty = True
                return True
        return False

    def cycle_replacement_category(self, from_text: str) -> str | None:
        """Advance a replacement's category to the next in CATEGORIES (wraps).
        Returns the new category name, or None if the entry doesn't exist."""
        for r in self.sc.replacements:
            if r.from_text == from_text:
                idx = sidecar.CATEGORIES.index(r.category) if r.category in sidecar.CATEGORIES else 0
                new_cat = sidecar.CATEGORIES[(idx + 1) % len(sidecar.CATEGORIES)]
                r.category = new_cat
                self.sidecar_dirty = True
                return new_cat
        return None

    def remove_replacement(self, from_text: str) -> bool:
        reps = config.get_replacements(self.doc)
        if from_text not in reps:
            return False
        del reps[from_text]
        config.set_replacements(self.doc, reps)
        self.sc.replacements = [r for r in self.sc.replacements if r.from_text != from_text]
        self.config_dirty = True
        self.sidecar_dirty = True
        return True

    def set_setting(self, path: str, value: object) -> None:
        """Idempotent set of any dotted config path.

        Creates intermediate sections if needed. If the resolved value already
        equals `value`, the write is skipped so `config_dirty` stays False —
        crucial for widgets that fire Changed events during programmatic
        population (e.g. Settings pane hydrating from state on mount)."""
        parts = path.split(".")
        node = self.doc
        for p in parts[:-1]:
            if p not in node:
                node[p] = tomlkit.table()
            node = node[p]
        key = parts[-1]
        if key in node and _toml_equals(node[key], value):
            return
        node[key] = value
        self.config_dirty = True

    # --- save ---

    def save(self) -> list[str]:
        """Persist both files. Returns the list of restart-sensitive paths
        that changed since the last load/save (empty if none). Sets
        `daemon_stale=True` whenever that list is non-empty — the caller
        uses the pre-save value of `daemon_stale` to decide whether this is
        the first-time transition that should raise a modal or just a
        subsequent save during an already-stale session (pill only).

        Sync version — runs `voxtype -c <tmp> config` on the calling thread.
        Use `save_async` from Textual / async contexts to keep the event loop
        responsive while validation runs.

        Write order is load-bearing: config.toml + metadata.json are the
        canonical source of truth, and both must commit before the
        derivative sync.json bundle is touched. A failed primary save
        propagates (existing behavior) and leaves sync.json untouched —
        better to let the next save regenerate than to advertise state
        that didn't commit locally, which another Syncthing peer could
        then pull as truth.
        """
        baseline_doc = tomlkit.parse(self.loaded_dump)
        config.safe_save(self.doc, self.config_path)
        sidecar.save_atomic(self.sc, self.sidecar_path)
        # Both primary saves succeeded — derive and write the portable
        # bundle. A write failure here (disk full, unwritable path, etc.)
        # is logged but never propagated: sync.json is a convenience
        # artifact, and reverting a successful canonical save because
        # we couldn't also update the sync cache would be worse than
        # leaving a stale sync.json that gets rewritten on next save.
        try:
            sync.write_sync_bundle(self.doc, self.sc)
        except OSError as e:
            _logger.warning("sync.json write failed: %s", e)
        restart_fields = config.diff_restart_sensitive(baseline_doc, self.doc)
        if restart_fields:
            self.daemon_stale = True
        self.loaded_dump = tomlkit.dumps(self.doc)
        self.config_dirty = False
        self.sidecar_dirty = False
        return restart_fields

    async def save_async(self) -> list[str]:
        return await asyncio.to_thread(self.save)
