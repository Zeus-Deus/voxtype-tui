# Restart-sensitive fields

Some config fields are read by the voxtype daemon **once at startup** and cached for the life of the process. Changing them requires `systemctl --user restart voxtype` for the change to take effect. Others are read **per-transcription** (by the `transcribe-worker` subprocess that spawns for each recording) and apply on the next dictation without a restart.

## Fields that need a restart

```
state_file                      # daemon opens this once
engine                          # which backend loads at start
whisper.model                   # model weights loaded into RAM at start
whisper.mode                    # local vs remote decided at start
whisper.backend                 # deprecated alias of the above
whisper.gpu_isolation           # subprocess topology baked in
whisper.remote_endpoint         # remote backend connection established at start
parakeet.model_type             # same pattern for each engine's model key
moonshine.model
sensevoice.model
paraformer.model
dolphin.model
omnilingual.model
hotkey.key                      # evdev listener registers at start
hotkey.modifiers
hotkey.mode
hotkey.enabled
audio.device                    # audio capture initialized at start
audio.sample_rate
audio.feedback.enabled          # feedback theme loaded at start
audio.feedback.theme
audio.feedback.volume
vad.enabled                     # VAD model loaded at start (if enabled)
vad.model
vad.threshold
```

## Fields that apply immediately

```
whisper.initial_prompt          # your Vocabulary list
whisper.language                # language hint
whisper.translate
text.replacements               # your Dictionary
text.spoken_punctuation
text.smart_auto_submit
output.mode                     # type / clipboard / paste
output.fallback_to_clipboard
output.auto_submit
output.type_delay_ms
output.post_process.*           # LLM cleanup command + timeout
output.notification.*
```

## How voxtype-tui uses this

After a successful save, the TUI compares the pre-save and post-save docs against the list above. If nothing restart-sensitive changed, it just shows a "Saved" toast and you're done.

If something restart-sensitive did change, **and** `systemctl --user is-active voxtype` reports `active`, the TUI pushes a `RestartModal` that lists the specific fields that changed and offers Restart / Later buttons. It never restarts the daemon automatically — you always decide.

If the daemon isn't running under systemd (e.g. you launched `voxtype daemon` manually), the modal doesn't appear. Handle the restart yourself.

## How we know which is which

Empirical, not documented by voxtype directly:

- Static analysis of the `/usr/bin/voxtype` binary — explicit error strings like `"Restart daemon to use new model: systemctl --user restart voxtype"` and the existence of a `voxtype setup model --restart` flag confirm the model/engine/hotkey category.
- `/proc/<voxtype-pid>/fd` inspection showed that `config.toml` is **not** held open by the running daemon — it's parsed at startup and closed. That means the fields not loaded at start are picked up fresh when the `transcribe-worker` subprocess parses the config on each recording.

See `voxtype_tui/config.py::RESTART_SENSITIVE_PATHS` for the authoritative list.
