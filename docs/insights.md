# Insights: usage stats, recent history, archive

voxtype-tui can count your dictations, keep a short history of what you said,
and (if you ask for it) save every dictation as a dataset. Everything stays on
your machine. Voxtype itself is not modified: voxtype-tui uses Voxtype's
standard hook settings.

## The switches

All in `~/.config/voxtype-tui/features.json` (the Omarchy panel and the
headless API below change it for you). A missing file means the defaults.

| Feature | Default | What is stored |
|---|---|---|
| Usage stats (`usage.enabled`) | on | Counters only, never text: dictations, words, measured seconds and the model, per day and hour. |
| Recent history (`history.enabled`, `history.keep_days`) | on, 30 days | The raw transcript and the final text, so you can copy or fix one later. Keep 7 / 30 / 90 / 365 days or forever (`null`). |
| Archive (`archive.enabled`, `dir`, `save_audio`, `save_text`) | off | A WAV and a JSON file per dictation, in a folder you choose. |
| Learn from corrections (`learning.*`) | on | See the auto-learn docs. |

Turning a switch off stops writing for that feature right away, without a restart.
History you already have stays until it expires or you clear it.

### Archive folder

If you turn the archive on without picking a folder, it uses `~/Documents/Voxtype`:

```
~/Documents/Voxtype/YYYY/MM/DD/<id>.wav    # a separate 16 kHz mono recording of your mic
~/Documents/Voxtype/YYYY/MM/DD/<id>.json   # the record below (+ text when "save text" is on)
~/Documents/Voxtype/index.jsonl            # one line per dictation
```

The WAV is recorded alongside Voxtype by `pw-record` (or `parec`) on the same
`audio.device`. It is not Voxtype's exact model input: it starts about 40 ms
after Voxtype starts recording and stops when Voxtype stops. The JSON's
`audio_info.start_offset_s` gives the measured offset for that dictation.
When a recording is cancelled, or VAD finds no speech, nothing is archived.

## Files

| Path | Content |
|---|---|
| `~/.config/voxtype-tui/features.json` | the switches, plus `managed_hooks` (which hook commands voxtype-tui wrote) |
| `~/.local/share/voxtype-tui/queue.jsonl` | records waiting to be counted (emptied after each dictation or on the next read) |
| `~/.local/share/voxtype-tui/usage.json` | counters, no text |
| `~/.local/share/voxtype-tui/history.jsonl` | recent dictations (text), pruned to `keep_days` |
| `~/.local/share/voxtype-tui/health.json`, `failures.jsonl` | last capture time and recent capture errors |
| `$XDG_RUNTIME_DIR/voxtype-tui/` | per-dictation scratch (RAM): recording start, the temp WAV, and `handoff/<id>.json`, one record per dictation for the archive and the learning watcher (removed once handled, or after 10 minutes) |

The files are created 0600 in 0700 directories. None of them are in `sync.json` or in Export.

Without `XDG_RUNTIME_DIR` the scratch dir is `/tmp/voxtype-tui-<uid>`. Because
another user could create that name first, it is used only when it is a real
directory (not a symlink) owned by you and not writable by anyone else;
otherwise the archive and the learning watcher skip that dictation and the
problem is listed in `health.failures` (usage still counts it). Cleaning old
scratch never follows symlinks.

## How it hooks into Voxtype

voxtype-tui sets these Voxtype settings only when a switch needs them, and only
if you have not set them yourself:

- `[output.post_process] command` runs `voxtype-tui-postprocess`, as before for vocabulary
  and dictionary users. It is the only program that sees the text. After the
  existing stages it adds one line to the queue (about 0.3 ms). Voxtype waits
  for this command, so the queue is never waited on for more than 50 ms: if
  something holds it longer, that one record is dropped and counted in
  `health.dropped`, and the text goes out unchanged. Deleting or clearing
  History never holds the queue for more than a rename. Once the text is
  written, the command does no fsync and no cleanup: the per-dictation
  handoff file is written without fsync, old handoffs are removed by a
  background process, and a capture error is logged by a background process,
  so a stalled disk cannot delay the dictation. The command is
  `command -v voxtype-tui-postprocess >/dev/null 2>&1 && exec voxtype-tui-postprocess; exec cat`.
  If voxtype-tui is removed, `cat` passes the text through unchanged. A bare
  command that exits without reading the text can kill the Voxtype 1.1.1 daemon
  (SIGPIPE, measured), so the old bare `voxtype-tui-postprocess` value is
  upgraded to this form when voxtype-tui rewires.
- `pre_recording_command`, for usage, history or archive: it notes when recording
  started (for measured durations) and which window was focused, and starts the
  archive recorder. Voxtype starts capturing before it runs this hook, so the hook
  cannot cost you words. The hook returns in about 13 ms. The rest runs in the background.
- `post_output_command`, for usage, history, the archive or the learning watcher.
  After each dictation it moves the queue into `usage.json` and `history.jsonl`, so
  nothing has to open the panel for the counts to stay current. Voxtype waits
  for this hook before it can record again, so the hook only claims this
  dictation's record (by id, before Voxtype can start the next one), starts a
  background process and returns. A background process that starts late still
  handles its own dictation, never a newer one.

The command it writes is safe if voxtype-tui is removed:

```
command -v voxtype-tui-hook >/dev/null 2>&1 && exec voxtype-tui-hook pre-recording; exit 0
```

If you already have your own `pre_recording_command` or `post_output_command`,
voxtype-tui leaves it alone. That holds when you set it while voxtype-tui is
saving, or while the TUI is open: a save checks that `config.toml` is still
what it read, and when it is not, it reloads the file, keeps your hook
commands and checks again before writing. This covers every command Voxtype
runs: `pre_recording_command`, `pre_output_command`, `post_output_command` and
the whole `[output.post_process]` table (command and `timeout_ms`). A TUI save
writes one of them over your edit only when you changed that same field in the
TUI. The feature that needs it shows as unavailable,
with the reason. Usage and history keep working with your own
`post_output_command`: once the queue passes 256 KiB, the post-process starts
one background drain (at most once a minute), so the queue never fills up and
drops dictations. With your own post-processing command (an LLM cleanup script,
say), usage and history are unavailable, because voxtype-tui never sees the text.

Changing these settings needs a daemon restart, like any Voxtype config change.
The panel's "Restart to apply" and the TUI's restart prompt cover it.
Uninstalling the package (pacman) removes the hook commands voxtype-tui wrote
and nothing else.

## What is not available, and why

- **Duration and words per minute** come from the moments Voxtype writes `recording`
  and `transcribing` to its state file. If the pre-recording hook is not ours, durations
  are `null`, never estimated, and "time saved" is not shown. The state file is
  wherever Voxtype's `state_file` setting puts it (`"auto"` or a path). With
  `state_file = "disabled"` durations are `null` (health says why) and the
  archive saves no audio, because nothing tells the recorder when you stopped.
- **The model** is read from `config.toml`. When the config is newer than the
  running daemon, the record says `model_uncertain: true`.
- **Dictations made before the upgrade** are not counted. voxtype-tui does not
  read the journal.

## Headless API (`voxtype_tui.insights`, stdlib only)

```python
from voxtype_tui import insights

insights.get_features()               # {features, available:{usage,history,archive,learning,everywhere}, reasons}
insights.set_features({"history": {"keep_days": 90}})   # + restart_needed, config_changed
insights.usage_summary(days=365)      # {enabled, days:[{date,dictations,words,seconds}], totals:{dictations,words,seconds,wpm,saved_seconds}, hours:[24], models, since, health}
insights.history_list(before=None, limit=20)            # {enabled, keep_days, items:[record], next}
insights.history_delete(record_id)    # {removed}
insights.history_clear()              # {removed}
```

`wpm` = words / measured minutes. `saved_seconds` = the time typing those words
at `usage.typing_wpm` (default 40) would have taken, minus the time you spent
speaking. Both are `null` until something has been measured.

`health` = `{last_capture_ts, dropped, failures, unavailable:[{feature, reason}],
pending_wiring}`. `pending_wiring` lists the features whose hook is not in
voxtype's config yet (`capture`, `duration`, `learning`), e.g. right after an
upgrade before anything saved the config; each also appears in `unavailable`
as "not connected to Voxtype yet". Re-saving any switch with `set_features`
wires them; the daemon then needs a restart.

A record:

```json
{"id": "20261106T203553Z-1a2b", "ts": "2026-11-06T20:35:53.628Z", "started_at": "…", "duration_s": 8.9,
 "raw": "…Omashi…", "final": "…Omarchy…", "words": 31, "chars": 212,
 "engine": "whisper", "model": "base.en", "language": "en", "model_uncertain": false,
 "app": {"class": "foot", "title": "…", "pid": 1234, "address": "0x…"},
 "changed_by": ["dictionary", "vocab", "learned"], "audio": "2026/11/06/20261106T203553Z-1a2b.wav"}
```

Command line: `voxtype-tui-hook --sync` re-applies the hook wiring for the
current switches. `voxtype-tui-hook --unwire` removes voxtype-tui's hooks.
