#!/usr/bin/env python
"""Smoke test for the config + sidecar layer.

Runs against the real ~/.config/voxtype/config.toml *read-only*. All mutations
happen against a temp copy. Exits 0 on pass, 1 on fail.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxtype_tui import config, sidecar  # noqa: E402


def main() -> int:
    print(f"real config:  {config.CONFIG_PATH}")
    print(f"real sidecar: {sidecar.SIDECAR_PATH} (exists={sidecar.SIDECAR_PATH.exists()})")
    print()

    doc = config.load()
    sc = sidecar.load()

    initial = config.get_initial_prompt(doc)
    reps = config.get_replacements(doc)

    print(f"initial_prompt:   {initial!r}")
    print(f"replacements:     {reps}")
    print(f"sidecar vocab:    {[v.phrase for v in sc.vocabulary]}")
    print(f"sidecar reps:     {[(r.from_text, r.category) for r in sc.replacements]}")
    print()

    vocab_rec, warns = sidecar.reconcile_vocab(sc.vocabulary, initial)
    for w in warns:
        print(f"  WARN(vocab): {w}")
    reps_rec, warns = sidecar.reconcile_replacements(sc.replacements, reps)
    for w in warns:
        print(f"  WARN(reps):  {w}")
    print(f"reconciled vocab: {[v.phrase for v in vocab_rec]}")
    print(f"reconciled reps:  {[(r.from_text, r.category) for r in reps_rec]}")
    print()

    with tempfile.TemporaryDirectory() as td:
        tmp_cfg = Path(td) / "config.toml"
        tmp_side = Path(td) / "metadata.json"
        shutil.copy(config.CONFIG_PATH, tmp_cfg)

        # 1. vocab + replacements round-trip
        doc2 = config.load(tmp_cfg)
        vocab2 = list(vocab_rec) + [
            sidecar.VocabEntry(phrase="Voxtype"),
            sidecar.VocabEntry(phrase="Claude Code"),
        ]
        reps2 = list(reps_rec) + [
            sidecar.ReplacementEntry(from_text="cloud code"),
            sidecar.ReplacementEntry(from_text="slash codemux release", category="Command"),
        ]
        config.set_initial_prompt(doc2, sidecar.build_initial_prompt(vocab2))
        config.set_replacements(doc2, {
            "cloud code": "Claude Code",
            "slash codemux release": "/codemux-release",
        })

        changed = config.diff_restart_sensitive(doc, doc2)
        print(f"vocab/reps edit → restart-sensitive diff: {changed}")
        assert changed == [], f"unexpected restart diff: {changed}"

        # 2. Flipping a restart-sensitive field is detected
        doc3 = config.load(tmp_cfg)
        doc3["whisper"]["model"] = "large-v3-turbo"
        changed = config.diff_restart_sensitive(doc, doc3)
        print(f"model flip → restart-sensitive diff:      {changed}")
        assert "whisper.model" in changed

        # 3. save → reload round-trip preserves our writes
        config.save_atomic(doc2, tmp_cfg)
        doc2b = config.load(tmp_cfg)
        assert config.get_initial_prompt(doc2b) == sidecar.build_initial_prompt(vocab2)
        assert config.get_replacements(doc2b) == {
            "cloud code": "Claude Code",
            "slash codemux release": "/codemux-release",
        }
        print(f"save+reload initial_prompt: {config.get_initial_prompt(doc2b)!r}")

        # 4. Sidecar save+load round-trip
        new_sc = sidecar.Sidecar(vocabulary=vocab2, replacements=reps2)
        sidecar.save_atomic(new_sc, tmp_side)
        reloaded = sidecar.load(tmp_side)
        assert [v.phrase for v in reloaded.vocabulary] == [v.phrase for v in vocab2]
        assert [(r.from_text, r.category) for r in reloaded.replacements] == \
               [(r.from_text, r.category) for r in reps2]
        print("sidecar save+load: OK")

        # 5. Empty-replacements: [text].replacements removed cleanly
        doc4 = config.load(tmp_cfg)
        config.set_replacements(doc4, {})
        config.save_atomic(doc4, tmp_cfg)
        doc4b = config.load(tmp_cfg)
        assert config.get_replacements(doc4b) == {}
        print("empty-replacements: OK")

        # 6. Clear initial_prompt
        doc5 = config.load(tmp_cfg)
        config.set_initial_prompt(doc5, None)
        config.save_atomic(doc5, tmp_cfg)
        doc5b = config.load(tmp_cfg)
        assert config.get_initial_prompt(doc5b) is None
        print("clear initial_prompt: OK")

        # 7. Reconcile against hand-edited config (user adds a replacement we
        #    don't know about → sidecar defaults it; user removes a sidecar
        #    entry → orphan gets dropped).
        handedit = config.load(tmp_cfg)
        config.set_replacements(handedit, {"foo": "bar"})  # one key we don't know
        config.save_atomic(handedit, tmp_cfg)
        handedit_reloaded = config.get_replacements(config.load(tmp_cfg))
        orphan_sc = [
            sidecar.ReplacementEntry(from_text="ghost", category="Command"),
            sidecar.ReplacementEntry(from_text="foo", category="Capitalization"),
        ]
        rec, warns = sidecar.reconcile_replacements(orphan_sc, handedit_reloaded)
        cats = {r.from_text: r.category for r in rec}
        assert cats == {"foo": "Capitalization"}, f"got {cats}"
        assert any("orphan" in w.lower() or "dropped" in w.lower() for w in warns)
        print(f"reconcile handedit:  {cats}, warns={warns}")

        # 8. Vocabulary divergence detection
        handedit2 = config.load(tmp_cfg)
        config.set_initial_prompt(handedit2, "Alpha, Beta, Gamma")
        config.save_atomic(handedit2, tmp_cfg)
        disk_initial = config.get_initial_prompt(config.load(tmp_cfg))
        stale_vocab = [sidecar.VocabEntry(phrase="Voxtype"),
                       sidecar.VocabEntry(phrase="Claude Code")]
        rec_vocab, warns = sidecar.reconcile_vocab(stale_vocab, disk_initial)
        assert [v.phrase for v in rec_vocab] == ["Alpha", "Beta", "Gamma"]
        assert warns, "expected a divergence warning"
        print(f"reconcile divergence: {[v.phrase for v in rec_vocab]}, warns={warns}")

        # 9. Comments survive all of the above
        orig_comments = sum(
            1 for ln in config.CONFIG_PATH.read_text().splitlines()
            if ln.lstrip().startswith("#")
        )
        after_comments = sum(
            1 for ln in tmp_cfg.read_text().splitlines()
            if ln.lstrip().startswith("#")
        )
        print(f"comments: original={orig_comments}, after={after_comments}")
        assert after_comments >= orig_comments, \
            f"lost comments: {orig_comments} → {after_comments}"

    print()
    print("=== smoke test PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
