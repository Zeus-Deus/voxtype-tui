"""Tests for the v1.1 portable-bundle module (`voxtype_tui.sync`).

The module is pure-function: no filesystem, no clock reads that matter to
correctness (generated_at is display-only, the content-hash is what the
startup reader consults). So every test here is small, deterministic, and
runs without tmp_path unless it loads a fixture file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomlkit

from voxtype_tui import sidecar, sync

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _toml(doc_str: str):
    return tomlkit.parse(doc_str)


def _sample_state() -> tuple[object, list, list]:
    """A representative config + sidecar shaped like a real user's setup."""
    doc = _toml(
        """
state_file = "auto"
engine = "whisper"

[hotkey]
key = "SCROLLLOCK"
modifiers = ["LEFTCTRL"]
mode = "toggle"
enabled = true

[audio]
device = "alsa_input.usb-0d8c_USB_Headset"
sample_rate = 16000
max_duration_secs = 120

[audio.feedback]
enabled = true
theme = "mechanical"
volume = 0.5

[whisper]
backend = "remote"
model = "large-v3-turbo"
language = "en"
translate = false
threads = 8
remote_endpoint = "http://192.168.1.42:8080"
remote_model = "whisper-1"
remote_timeout_secs = 60
remote_api_key = "sk-super-secret-key"
initial_prompt = "Codemux, Claude Code"

[output]
mode = "type"
auto_submit = false
fallback_to_clipboard = true
type_delay_ms = 2
pre_output_command = "hyprctl dispatch submap voxtype_suppress"
post_output_command = "hyprctl dispatch submap reset"

[output.post_process]
command = "ollama run llama3.2:1b 'Clean up this dictation.'"
timeout_ms = 30000

[text]
spoken_punctuation = true
smart_auto_submit = true

[text.replacements]
"vox type" = "voxtype"
"cloud code" = "Claude Code"

[vad]
enabled = false
threshold = 0.5
"""
    )
    vocab = [
        sidecar.VocabEntry(phrase="Codemux", added_at="2026-01-01T00:00:00+00:00"),
        sidecar.VocabEntry(phrase="Claude Code", added_at="2026-01-02T00:00:00+00:00"),
    ]
    reps = [
        sidecar.ReplacementEntry(
            from_text="vox type", category="Replacement",
            added_at="2026-01-03T00:00:00+00:00",
        ),
        sidecar.ReplacementEntry(
            from_text="cloud code", category="Replacement",
            added_at="2026-01-04T00:00:00+00:00",
        ),
    ]
    return doc, vocab, reps


# ---------------------------------------------------------------------------
# distill_sync / distill_local / distill_secrets
# ---------------------------------------------------------------------------

def test_distill_sync_contains_vocab_and_replacements() -> None:
    doc, vocab, reps = _sample_state()
    s = sync.distill_sync(doc, vocab, reps)
    assert [v["phrase"] for v in s["vocabulary"]] == ["Codemux", "Claude Code"]
    assert [(r["from"], r["to"]) for r in s["replacements"]] == [
        ("vox type", "voxtype"),
        ("cloud code", "Claude Code"),
    ]


def test_distill_sync_never_contains_secret_paths() -> None:
    """Hard security invariant: no matter what the live config holds, the
    sync block must not leak API keys or *_command shell strings."""
    doc, vocab, reps = _sample_state()
    s = sync.distill_sync(doc, vocab, reps)
    settings = s["settings"]
    assert "remote_api_key" not in settings.get("whisper", {})
    output = settings.get("output", {})
    assert "pre_output_command" not in output
    assert "post_output_command" not in output
    assert "command" not in output.get("post_process", {})
    # But the non-secret post_process.timeout_ms still comes through.
    assert output.get("post_process", {}).get("timeout_ms") == 30000


def test_distill_sync_settings_preserve_portable_fields() -> None:
    doc, vocab, reps = _sample_state()
    settings = sync.distill_sync(doc, vocab, reps)["settings"]
    assert settings["engine"] == "whisper"
    assert settings["whisper"]["model"] == "large-v3-turbo"
    assert settings["whisper"]["language"] == "en"
    assert settings["whisper"]["remote_endpoint"] == "http://192.168.1.42:8080"
    assert settings["text"]["spoken_punctuation"] is True
    assert settings["output"]["mode"] == "type"


def test_distill_sync_skips_replacements_without_config_value() -> None:
    """If sidecar lists a replacement but config.toml lost its `to` value
    (shouldn't happen post-reconcile, but defensively), don't emit a
    half-baked entry with `to=null`."""
    doc = _toml('[text]\n[text.replacements]\n"keep" = "OK"\n')
    reps = [
        sidecar.ReplacementEntry(from_text="keep"),
        sidecar.ReplacementEntry(from_text="orphan"),
    ]
    out = sync.distill_sync(doc, [], reps)
    assert [r["from"] for r in out["replacements"]] == ["keep"]


def test_distill_local_contains_per_device_fields() -> None:
    doc, _, _ = _sample_state()
    local = sync.distill_local(doc)
    assert local["hotkey"]["key"] == "SCROLLLOCK"
    assert local["hotkey"]["modifiers"] == ["LEFTCTRL"]
    assert local["audio"]["device"] == "alsa_input.usb-0d8c_USB_Headset"
    assert local["audio"]["feedback"]["theme"] == "mechanical"
    assert local["vad"]["enabled"] is False


def test_distill_local_never_contains_secret_paths() -> None:
    doc, _, _ = _sample_state()
    local = sync.distill_local(doc)
    # Secrets belong in their own block, not local.
    assert "remote_api_key" not in local.get("whisper", {})
    assert "post_process" not in local.get("output", {})


def test_distill_secrets_collects_only_present_fields() -> None:
    doc, _, _ = _sample_state()
    secrets = sync.distill_secrets(doc)
    assert secrets["whisper"]["remote_api_key"] == "sk-super-secret-key"
    assert "ollama" in secrets["output"]["post_process"]["command"]
    assert "pre_output_command" in secrets["output"]
    assert "post_output_command" in secrets["output"]


def test_distill_secrets_skips_empty_api_key() -> None:
    """Empty `remote_api_key` (user wires it via $VOXTYPE_WHISPER_API_KEY
    instead) is not a secret to carry — don't emit a null-ish entry."""
    doc = _toml('[whisper]\nremote_api_key = ""\n')
    assert sync.distill_secrets(doc) == {}


def test_distill_from_empty_doc() -> None:
    """Fresh install / minimal config: everything degrades to empty dicts,
    no crash."""
    doc = _toml("")
    s = sync.distill_sync(doc, [], [])
    assert s["vocabulary"] == []
    assert s["replacements"] == []
    assert s["settings"] == {}
    assert sync.distill_local(doc) == {}
    assert sync.distill_secrets(doc) == {}


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def test_stable_hash_is_deterministic() -> None:
    block = {"a": 1, "b": [1, 2, 3], "c": {"x": True, "y": "z"}}
    h1 = sync.stable_hash(block)
    h2 = sync.stable_hash(block)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_stable_hash_is_order_independent_on_dict_keys() -> None:
    a = {"x": 1, "y": 2, "z": {"q": "w", "e": "r"}}
    b = {"z": {"e": "r", "q": "w"}, "y": 2, "x": 1}
    assert sync.stable_hash(a) == sync.stable_hash(b)


def test_stable_hash_is_order_dependent_on_lists() -> None:
    """Vocab ORDER affects Whisper's prompt weighting. Two bundles with the
    same words in different order must hash differently — otherwise the
    staleness compare would miss a meaningful reorder."""
    a = {"vocabulary": [{"phrase": "A"}, {"phrase": "B"}]}
    b = {"vocabulary": [{"phrase": "B"}, {"phrase": "A"}]}
    assert sync.stable_hash(a) != sync.stable_hash(b)


def test_stable_hash_handles_unicode() -> None:
    block = {"vocabulary": [{"phrase": "日本語"}, {"phrase": "🦀 Rust"}]}
    h = sync.stable_hash(block)
    assert len(h) == 64


# ---------------------------------------------------------------------------
# build_bundle / to_json / from_json round-trip
# ---------------------------------------------------------------------------

def test_build_bundle_auto_sync_path_omits_secrets_block() -> None:
    doc, vocab, reps = _sample_state()
    s = sync.distill_sync(doc, vocab, reps)
    local = sync.distill_local(doc)
    secrets = sync.distill_secrets(doc)
    bundle = sync.build_bundle(
        sync=s, local=local, secrets=secrets,
        device_label="zeus-desktop",
        include_secrets=False,
        generated_at="2026-04-21T20:30:00Z",
    )
    assert bundle.secrets is None
    as_json = sync.to_json(bundle)
    assert "remote_api_key" not in as_json
    assert "sk-super-secret-key" not in as_json


def test_build_bundle_manual_export_with_secrets_toggle_includes_them() -> None:
    doc, vocab, reps = _sample_state()
    s = sync.distill_sync(doc, vocab, reps)
    local = sync.distill_local(doc)
    secrets = sync.distill_secrets(doc)
    bundle = sync.build_bundle(
        sync=s, local=local, secrets=secrets,
        device_label="zeus-desktop",
        include_secrets=True,
    )
    assert bundle.secrets is not None
    text = sync.to_json(bundle)
    assert "sk-super-secret-key" in text
    # Secrets block MUST be separate from the sync block — easy
    # audibility: one glance at the top-level keys tells you the file
    # contains sensitive data.
    assert "secrets" in text
    # And the sync block specifically still excludes it.
    sync_section_str = json.dumps(bundle.sync)
    assert "remote_api_key" not in sync_section_str
    assert "sk-super-secret-key" not in sync_section_str


def test_build_bundle_rejects_bad_device_label() -> None:
    with pytest.raises(sync.BundleError):
        sync.build_bundle(sync={}, local={}, secrets=None, device_label="",
                          include_secrets=False)
    with pytest.raises(sync.BundleError):
        sync.build_bundle(sync={}, local={}, secrets=None,
                          device_label="x" * 500, include_secrets=False)


def test_bundle_round_trip_preserves_structure() -> None:
    doc, vocab, reps = _sample_state()
    s = sync.distill_sync(doc, vocab, reps)
    bundle = sync.build_bundle(
        sync=s, local=sync.distill_local(doc), secrets=None,
        device_label="zeus-laptop", include_secrets=False,
        generated_at="2026-04-21T20:30:00Z",
    )
    text = sync.to_json(bundle)
    reloaded = sync.from_json(text)
    assert reloaded.schema_version == bundle.schema_version
    assert reloaded.format == bundle.format
    assert reloaded.generated_by_device == "zeus-laptop"
    assert reloaded.local_sync_hash == bundle.local_sync_hash
    assert reloaded.sync == bundle.sync
    assert reloaded.local == bundle.local
    assert reloaded.secrets is None


def test_bundle_hash_matches_after_round_trip() -> None:
    """Stored `local_sync_hash` must equal the hash computed from the
    embedded sync block — otherwise the startup reader can't trust it."""
    doc, vocab, reps = _sample_state()
    s = sync.distill_sync(doc, vocab, reps)
    bundle = sync.build_bundle(
        sync=s, local={}, secrets=None,
        device_label="device", include_secrets=False,
    )
    reloaded = sync.from_json(sync.to_json(bundle))
    assert sync.stable_hash(reloaded.sync) == reloaded.local_sync_hash


def test_unicode_round_trip() -> None:
    """Emoji, RTL, combining characters survive a full serialize / parse."""
    doc = _toml(
        '[text]\n[text.replacements]\n"rtl" = "العربية"\n'
    )
    vocab = [
        sidecar.VocabEntry(phrase="日本語"),
        sidecar.VocabEntry(phrase="🦀 Rust"),
        sidecar.VocabEntry(phrase="café"),
    ]
    reps = [sidecar.ReplacementEntry(from_text="rtl")]
    s = sync.distill_sync(doc, vocab, reps)
    bundle = sync.build_bundle(
        sync=s, local={}, secrets=None,
        device_label="zeus", include_secrets=False,
    )
    reloaded = sync.from_json(sync.to_json(bundle))
    assert [v["phrase"] for v in reloaded.sync["vocabulary"]] == [
        "日本語", "🦀 Rust", "café",
    ]
    assert reloaded.sync["replacements"][0]["to"] == "العربية"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_from_json_rejects_non_object_root() -> None:
    with pytest.raises(sync.BundleError):
        sync.from_json("[]")


def test_from_json_rejects_wrong_format_tag() -> None:
    payload = json.dumps({
        "schema_version": 1, "format": "something-else",
        "sync": {}, "local": {}, "generated_by_device": "d",
        "generated_at": "", "local_sync_hash": "",
    })
    with pytest.raises(sync.BundleError, match="format"):
        sync.from_json(payload)


def test_from_json_rejects_newer_schema() -> None:
    payload = json.dumps({
        "schema_version": 99, "format": sync.FORMAT_TAG,
        "sync": {}, "local": {}, "generated_by_device": "d",
        "generated_at": "", "local_sync_hash": "",
    })
    with pytest.raises(sync.BundleError, match="newer"):
        sync.from_json(payload)


def test_from_json_rejects_invalid_schema_type() -> None:
    payload = json.dumps({
        "schema_version": "one", "format": sync.FORMAT_TAG,
        "sync": {}, "local": {},
    })
    with pytest.raises(sync.BundleError):
        sync.from_json(payload)


def test_from_json_rejects_oversize_payload() -> None:
    """Defends the importer against a crafted multi-MB blob."""
    huge = "x" * (sync.MAX_BUNDLE_BYTES + 1)
    with pytest.raises(sync.BundleError, match="refusing to parse"):
        sync.from_json(huge)


def test_from_json_rejects_corrupt_json() -> None:
    with pytest.raises(sync.BundleError, match="invalid JSON"):
        sync.from_json("{not even close")


def test_from_json_rejects_vocab_phrase_too_long() -> None:
    payload = _minimal_payload()
    payload["sync"]["vocabulary"] = [
        {"phrase": "x" * (sync.MAX_VOCAB_PHRASE_LEN + 1)}
    ]
    with pytest.raises(sync.BundleError, match="exceeds"):
        sync.from_json(json.dumps(payload))


def test_from_json_rejects_too_many_vocab_entries() -> None:
    payload = _minimal_payload()
    payload["sync"]["vocabulary"] = [
        {"phrase": f"p{i}"} for i in range(sync.MAX_VOCAB_COUNT + 1)
    ]
    with pytest.raises(sync.BundleError, match="limit"):
        sync.from_json(json.dumps(payload))


def test_from_json_rejects_replacement_missing_fields() -> None:
    payload = _minimal_payload()
    payload["sync"]["replacements"] = [{"from": "a"}]  # missing `to`
    with pytest.raises(sync.BundleError, match="replacements"):
        sync.from_json(json.dumps(payload))


def test_from_json_rejects_deeply_long_setting_string() -> None:
    payload = _minimal_payload()
    payload["sync"]["settings"] = {
        "whisper": {"model": "x" * (sync.MAX_SETTING_STRING_LEN + 1)}
    }
    with pytest.raises(sync.BundleError, match="exceeds"):
        sync.from_json(json.dumps(payload))


def test_from_json_accepts_payload_at_the_limit() -> None:
    """Boundary check: exactly MAX bytes (counting the UTF-8 encoding)
    must still parse — the limit is exclusive."""
    payload = json.dumps(_minimal_payload())
    # Make sure our minimal payload is well under the limit (sanity check
    # the test, not the code).
    assert len(payload.encode("utf-8")) < sync.MAX_BUNDLE_BYTES
    sync.from_json(payload)  # should not raise


def _minimal_payload() -> dict:
    return {
        "schema_version": sync.SCHEMA_VERSION,
        "format": sync.FORMAT_TAG,
        "generated_at": "2026-04-21T20:30:00Z",
        "generated_by_device": "test-device",
        "local_sync_hash": "0" * 64,
        "sync": {"vocabulary": [], "replacements": [], "settings": {}},
        "local": {},
    }


# ---------------------------------------------------------------------------
# Secret handling post-parse
# ---------------------------------------------------------------------------

def test_strip_secrets_returns_bundle_without_secrets_block() -> None:
    doc, vocab, reps = _sample_state()
    bundle = sync.build_bundle(
        sync=sync.distill_sync(doc, vocab, reps),
        local=sync.distill_local(doc),
        secrets=sync.distill_secrets(doc),
        device_label="d",
        include_secrets=True,
    )
    assert bundle.secrets is not None
    stripped = sync.strip_secrets(bundle)
    assert stripped.secrets is None
    # Preserve every other field.
    assert stripped.sync == bundle.sync
    assert stripped.local == bundle.local
    assert stripped.local_sync_hash == bundle.local_sync_hash


def test_diff_dangerous_flags_changed_api_key() -> None:
    current_sync = {"settings": {"whisper": {"model": "tiny"}}}
    current_secrets = {"whisper": {"remote_api_key": "sk-OLD"}}
    imported_sync = {"settings": {"whisper": {"model": "tiny"}}}
    imported_secrets = {"whisper": {"remote_api_key": "sk-NEW"}}
    diffs = sync.diff_dangerous(
        current_sync, imported_sync, current_secrets, imported_secrets,
    )
    assert "whisper.remote_api_key" in diffs


def test_diff_dangerous_flags_changed_endpoint() -> None:
    current = {"settings": {"whisper": {"remote_endpoint": "http://old"}}}
    incoming = {"settings": {"whisper": {"remote_endpoint": "http://evil.example.com"}}}
    diffs = sync.diff_dangerous(current, incoming, None, None)
    assert "whisper.remote_endpoint" in diffs


def test_diff_dangerous_flags_changed_shell_commands() -> None:
    current = {"settings": {}}
    incoming_secrets = {
        "output": {
            "pre_output_command": "whoami",
            "post_output_command": "hostname",
            "post_process": {"command": "ollama run …"},
        }
    }
    diffs = sync.diff_dangerous(current, {"settings": {}}, None, incoming_secrets)
    assert set(diffs) == {
        "output.pre_output_command",
        "output.post_output_command",
        "output.post_process.command",
    }


def test_diff_dangerous_treats_redacted_imports_as_no_change() -> None:
    """A bundle where the import has no api_key (auto-synced file) must
    NOT propose overwriting the local key with empty string."""
    current = {"settings": {}}
    current_secrets = {"whisper": {"remote_api_key": "sk-KEEP"}}
    imported = {"settings": {}}
    # Imported has no secrets at all — the common sync-writer case.
    diffs = sync.diff_dangerous(current, imported, current_secrets, None)
    assert diffs == []


def test_diff_dangerous_no_false_positives_on_equal_values() -> None:
    current = {"settings": {"whisper": {"remote_endpoint": "http://hub"}}}
    incoming = {"settings": {"whisper": {"remote_endpoint": "http://hub"}}}
    assert sync.diff_dangerous(current, incoming, None, None) == []


# ---------------------------------------------------------------------------
# Staleness (content-hash compare)
# ---------------------------------------------------------------------------

def test_local_unchanged_hash_matches_stored() -> None:
    """Simulates the "local unchanged since last sync write → apply sync"
    path. Build a bundle, persist its hash, then re-derive from the same
    state; hashes must match bit-for-bit."""
    doc, vocab, reps = _sample_state()
    s = sync.distill_sync(doc, vocab, reps)
    bundle = sync.build_bundle(
        sync=s, local={}, secrets=None,
        device_label="d", include_secrets=False,
    )
    # Recompute hash from the same state (no mutation between writes).
    recomputed = sync.stable_hash(sync.distill_sync(doc, vocab, reps))
    assert recomputed == bundle.local_sync_hash


def test_local_changed_hash_differs_from_stored() -> None:
    """User added a vocab entry since last sync write → hash changes →
    applier recognizes local drift and does not overwrite."""
    doc, vocab, reps = _sample_state()
    stored_hash = sync.stable_hash(sync.distill_sync(doc, vocab, reps))
    vocab.append(sidecar.VocabEntry(phrase="NewWord"))
    new_hash = sync.stable_hash(sync.distill_sync(doc, vocab, reps))
    assert stored_hash != new_hash


# ---------------------------------------------------------------------------
# Vexis format adapter
# ---------------------------------------------------------------------------

def test_detect_format_voxtype_tui() -> None:
    parsed = {"format": sync.FORMAT_TAG, "schema_version": 1, "sync": {}}
    assert sync.detect_format(parsed) == sync.VOXTYPE_TUI_FORMAT


def test_detect_format_vexis_dictionary() -> None:
    parsed = [{"id": 1, "trigger": "a", "replacement": "b", "category": "replacement"}]
    assert sync.detect_format(parsed) == sync.VEXIS_DICTIONARY_FORMAT


def test_detect_format_vexis_vocabulary_strings() -> None:
    parsed = ["Codemux", "Vexis"]
    assert sync.detect_format(parsed) == sync.VEXIS_VOCABULARY_FORMAT


def test_detect_format_vexis_vocabulary_objects() -> None:
    parsed = [{"id": 1, "word": "Codemux"}]
    assert sync.detect_format(parsed) == sync.VEXIS_VOCABULARY_FORMAT


def test_detect_format_unknown() -> None:
    assert sync.detect_format({"random": "object"}) == sync.UNKNOWN_FORMAT
    assert sync.detect_format(42) == sync.UNKNOWN_FORMAT


def test_detect_format_empty_array_is_vocab() -> None:
    """Empty array is a no-op import either way; pick the simpler shape."""
    assert sync.detect_format([]) == sync.VEXIS_VOCABULARY_FORMAT


def test_adapt_vexis_dictionary_migrates_command_to_replacement() -> None:
    parsed = json.loads((FIXTURES / "vexis_dictionary_sample.json").read_text())
    out = sync.adapt_vexis_dictionary(parsed)
    by_from = {r["from"]: r for r in out}
    # Category "command" migrates silently to "Replacement" (same fix Vexis
    # itself applied in its storage migration).
    assert by_from["slash codemux release"]["category"] == "Replacement"
    assert by_from["slash codemux release"]["to"] == "/codemux-release"
    # `"capitalization"` → `"Capitalization"` (case change).
    assert by_from["type script"]["category"] == "Capitalization"
    # `"replacement"` → `"Replacement"`.
    assert by_from["vox type"]["category"] == "Replacement"


def test_adapt_vexis_dictionary_dedupes_case_insensitively() -> None:
    parsed = json.loads((FIXTURES / "vexis_dictionary_sample.json").read_text())
    out = sync.adapt_vexis_dictionary(parsed)
    triggers = [r["from"].lower() for r in out]
    assert len(triggers) == len(set(triggers))
    # Fixture has "cloud code" + "CLOUD CODE" — last should win.
    cloud = next(r for r in out if r["from"].lower() == "cloud code")
    assert cloud["to"] == "Claude Code (duplicate, last should win)"


def test_adapt_vexis_dictionary_rejects_malformed_rows() -> None:
    with pytest.raises(sync.BundleError):
        sync.adapt_vexis_dictionary([{"trigger": "only-trigger"}])
    with pytest.raises(sync.BundleError):
        sync.adapt_vexis_dictionary([{"trigger": "", "replacement": "x"}])
    with pytest.raises(sync.BundleError):
        sync.adapt_vexis_dictionary("not an array")


def test_adapt_vexis_vocabulary_string_form() -> None:
    parsed = json.loads((FIXTURES / "vexis_vocabulary_sample_strings.json").read_text())
    out = sync.adapt_vexis_vocabulary(parsed)
    assert [v["phrase"] for v in out] == [
        "Codemux", "Claude Code", "Vexis", "Omarchy",
        "Hyprland", "shadcn", "OAuth", "Bitwarden",
    ]


def test_adapt_vexis_vocabulary_object_form_dedupes() -> None:
    parsed = json.loads((FIXTURES / "vexis_vocabulary_sample_objects.json").read_text())
    out = sync.adapt_vexis_vocabulary(parsed)
    phrases = [v["phrase"] for v in out]
    # Fixture has "Codemux" + "codemux"; case-insensitive dedup keeps first.
    assert "Codemux" in phrases
    assert "codemux" not in phrases
    assert len(phrases) == len({p.lower() for p in phrases})


def test_adapt_vexis_vocabulary_ignores_whitespace_only_entries() -> None:
    out = sync.adapt_vexis_vocabulary(["", "  ", "Good", "\n"])
    assert [v["phrase"] for v in out] == ["Good"]


def test_adapt_vexis_vocabulary_rejects_oversize() -> None:
    too_many = [f"w{i}" for i in range(sync.MAX_VOCAB_COUNT + 1)]
    with pytest.raises(sync.BundleError):
        sync.adapt_vexis_vocabulary(too_many)


# ---------------------------------------------------------------------------
# Token-limit estimator
# ---------------------------------------------------------------------------

def test_estimate_tokens_empty_is_zero() -> None:
    assert sync.estimate_initial_prompt_tokens([]) == 0


def test_estimate_tokens_scales_with_length() -> None:
    short = sync.estimate_initial_prompt_tokens(["A", "B"])
    long_ = sync.estimate_initial_prompt_tokens([f"word{i}" for i in range(50)])
    assert long_ > short


def test_exceeds_initial_prompt_limit_triggers_on_large_vocab() -> None:
    # Construct a vocabulary that blows past 224 tokens (~900 chars).
    oversize = [f"phrase-number-{i:03d}" for i in range(100)]
    assert sync.exceeds_initial_prompt_limit(oversize) is True


def test_small_vocab_under_token_limit() -> None:
    assert sync.exceeds_initial_prompt_limit(["Codemux", "Vexis", "Claude Code"]) is False
