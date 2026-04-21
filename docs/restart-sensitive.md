# Restart-sensitive fields

Most config fields are read by the voxtype daemon **once at startup** and cached for the life of the process. Changing them requires `systemctl --user restart voxtype` for the change to take effect. A few fields (language hint, output mode, typing delay) do apply immediately on the next transcription without a restart.

## Fields that need a restart

```
state_file
engine
whisper.model                   # model weights loaded into RAM at start
whisper.mode                    # local vs remote decided at start
whisper.backend                 # deprecated alias of the above
whisper.gpu_isolation
whisper.remote_endpoint
whisper.initial_prompt          # your Vocabulary — read into the text-layer cache
parakeet.model_type             # per-engine model keys
moonshine.model
sensevoice.model
paraformer.model
dolphin.model
omnilingual.model
hotkey.key                      # evdev listener registers at start
hotkey.modifiers
hotkey.mode
hotkey.enabled
audio.device
audio.sample_rate
audio.feedback.enabled
audio.feedback.theme
audio.feedback.volume
vad.enabled
vad.model
vad.threshold
text.replacements               # your Dictionary — cached alongside the prompt
text.spoken_punctuation
text.smart_auto_submit
```

## Fields that apply immediately

```
whisper.language
whisper.translate
output.mode                     # type / clipboard / paste
output.fallback_to_clipboard
output.auto_submit
output.type_delay_ms
output.post_process.*
output.notification.*
```

## How voxtype-tui communicates this

After every save, the TUI compares the pre-save and post-save docs against the restart-sensitive list. If anything flagged changed, three things happen:

1. **First save that creates staleness** — a `RestartModal` lists the specific fields that changed and offers **Restart now** / **Later**. Picking Restart runs `systemctl --user restart voxtype` asynchronously and clears the state. Picking Later leaves the daemon stale.
2. **A persistent pill** appears in the header: `⚠ Daemon restart needed (Ctrl+Shift+R)`. It stays visible across tab switches for the rest of the session — no more dismissing a modal and forgetting the daemon is stale. Click the pill, or press **ctrl+shift+r** from anywhere, to restart.
3. **Subsequent saves while stale** just update dirty indicators + toast `"Saved — daemon still needs restart (ctrl+shift+r)"`. The modal doesn't re-appear for every save; the pill is the persistent signal.

Once the daemon is restarted (via the TUI or externally), the pill clears and the next restart-sensitive save can fire a fresh modal.

If the daemon isn't running under systemd (e.g. you launched `voxtype daemon` manually), the modal and pill don't appear — restart it yourself when you want the new config active.

## Design note

An earlier version of this file claimed `whisper.initial_prompt` and `text.*` were read per-transcription. That was wrong — the daemon caches those alongside the model weights at startup. The authoritative list lives at `voxtype_tui/config.py::RESTART_SENSITIVE_PATHS`; when in doubt, a field is added there. A false-positive restart prompt is much cheaper than silent staleness where you save a new vocabulary word and the daemon keeps using the old prompt.
