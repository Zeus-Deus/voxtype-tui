"""Application state: the loaded config doc + sidecar, plus dirty flags.

Centralizes mutations so the app doesn't have to remember to flip dirty bits
at every call site. Keeps us from paying to re-serialize on every keystroke —
the baseline (`loaded_dump`) is captured at load time and only updated on save.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
from tomlkit import TOMLDocument

from . import config, sidecar


@dataclass
class AppState:
    doc: TOMLDocument
    sc: sidecar.Sidecar
    config_path: Path
    sidecar_path: Path
    loaded_dump: str
    config_dirty: bool = False
    sidecar_dirty: bool = False
    reconcile_warnings: list[str] = field(default_factory=list)

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

        return cls(
            doc=doc,
            sc=sc,
            config_path=config_path,
            sidecar_path=sidecar_path,
            loaded_dump=tomlkit.dumps(doc),
            sidecar_dirty=sidecar_dirty,
            reconcile_warnings=w1 + w2,
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
        if category not in sidecar.CATEGORIES:
            category = sidecar.DEFAULT_CATEGORY
        reps = config.get_replacements(self.doc)
        reps[from_text] = to_text
        config.set_replacements(self.doc, reps)
        for r in self.sc.replacements:
            if r.from_text == from_text:
                r.category = category
                break
        else:
            self.sc.replacements.append(
                sidecar.ReplacementEntry(from_text=from_text, category=category)
            )
        self.config_dirty = True
        self.sidecar_dirty = True

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
        """Set any dotted config path. Creates intermediate sections if needed."""
        parts = path.split(".")
        node = self.doc
        for p in parts[:-1]:
            if p not in node:
                node[p] = tomlkit.table()
            node = node[p]
        node[parts[-1]] = value
        self.config_dirty = True

    # --- save ---

    def save(self) -> list[str]:
        """Persist both files. Returns the list of restart-sensitive paths
        that changed since the last load/save (empty if none)."""
        baseline_doc = tomlkit.parse(self.loaded_dump)
        config.safe_save(self.doc, self.config_path)
        sidecar.save_atomic(self.sc, self.sidecar_path)
        restart_fields = config.diff_restart_sensitive(baseline_doc, self.doc)
        self.loaded_dump = tomlkit.dumps(self.doc)
        self.config_dirty = False
        self.sidecar_dirty = False
        return restart_fields
