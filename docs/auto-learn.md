# Auto-learn: Voxtype learns from your corrections

When Voxtype mishears a word and you fix it, voxtype-tui remembers the fix
and applies it to later dictations. It works out of the box, entirely
offline, without AI and without restarting Voxtype.

Your own vocabulary and dictionary are never changed by learning. Learned
words live in their own list, which you can review, keep, reject or reset at
any time.

## Where it learns

| Where you fix the word | How | Needs |
|---|---|---|
| **History › Fix a word** (panel) | You correct the text of a past dictation | nothing; works for every app, terminals and Claude Code included |
| **In the app, right after dictating** | voxtype-tui watches the text field for up to 30 s and sees your edit | the app shows its text to the accessibility bus (below) |

Apps that work without any setup: GTK apps (GNOME Text Editor, gedit,
zenity, most GNOME apps). With **Learn everywhere** on: Chromium, Brave,
Chrome and Electron apps (Obsidian and others) and Qt apps. Terminals (foot,
Alacritty, kitty, Ghostty, WezTerm) never show their text, so use History ›
Fix there. Password fields and password managers (1Password, Bitwarden,
KeePassXC, …) are never watched.

## What it learns

| Kind | Example | What happens in later dictations |
|---|---|---|
| sounds like | `Omashi → Omarchy` | Every "Omashi" becomes "Omarchy"; close variants ("Omachi") too. Used when the misheard word is not an English word. |
| spelling | `Austin → Austen` | A hint only: close misspellings are fixed, but the real word "Austin" is left alone. |
| replace | `cloud → Claude` | Every "cloud" becomes "Claude". Only from an explicit choice in History › Fix. |

Telling "sounds like" from "spelling" needs the English word list
(cracklib or `/usr/share/dict/words`). Without one every correction is learned as a
spelling, unless you pick the kind yourself in History › Fix.

A correction is ignored when it does not look like a mishearing:

- more than 3 words changed, or more than 30 % of the words (50 % for an
  explicit Fix): that is a rewrite;
- the words are too different ("Tuesday → Friday" is an edit, not a mishearing);
- the new word is an ordinary lowercase English word (a grammar fix), has
  2 letters or fewer, or is a number;
- your own vocabulary or dictionary already covers it;
- you rejected it before (rejected words are never learned again).

An explicit Fix is learned at once. An in-app correction is learned at once
when it is unambiguous (one word replaced, the old word is not English and
sounds like the new one, e.g. Omashi → Omarchy). Otherwise it stays
**suggested** until you make the same fix twice within 30 days, or accept it
in the panel. The same rule holds for each misheard form on its own: once
"Omashi → Omarchy" is learned, an in-app "Omarion → Omarchy" is only
applied after you make it twice (or accept the entry), because it is not
close enough to count the first time. Typing more text after the dictation is fine; only the
dictated words are compared.

When a word is learned you get a notification, "Vocabulary learned — Added
"Omarchy" from your correction." Click it within 10 s to undo.

## Your words always win

Learned entries run after your dictionary and your vocabulary corrector.
They never change text that your own rules produced, nor occurrences of
your own words, nor URLs, paths, file names and identifiers. Your own
vocabulary prompt (`[whisper] initial_prompt`) is never touched, so learning
needs no restart and is fully reversible.

To make a learned word one of your own, **promote** it: a spelling becomes a
vocabulary word, a sounds-like/replace entry becomes a dictionary
replacement. From then on it is yours and learning leaves it alone.
Promotions run one at a time (under the learned-words lock), so promoting
several words at once never loses one.

## Learn everywhere

Some apps only expose their text when asked. **Learn everywhere** is one
switch; before it changes anything it shows you the exact files and lines:

- `~/.config/environment.d/90-voxtype-tui-accessibility.conf` (new file):
  `NO_AT_BRIDGE=0`, `QT_ACCESSIBILITY=1`, `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`,
  `ACCESSIBILITY_ENABLED=1`.
- `--force-renderer-accessibility`, under a marker comment, in the flags file
  of each installed Chromium-family browser and Electron:
  `chromium-flags.conf`, `brave-flags.conf`, `chrome-flags.conf`,
  `electronNN-flags.conf` (or your `electron-flags.conf` if you have one),
  `obsidian/user-flags.conf`.

Chromium and Electron need both the flag and the variable. Apps you start
after turning it on pick it up (the running session gets the variables and
the accessibility bus is switched on); apps that were already running need a
restart, and everything is in place after your next login.

Turning it off removes only the lines voxtype-tui added. Changes you made to
those files later are kept; a file it created and nothing else uses is
deleted. Symlinked flag files are left alone (add the flag yourself).

Cost, measured in an Omarchy VM: Chromium idle CPU unchanged (0.20 vs 0.23 %
of one core over 20 s); Chromium memory about 190 MB higher (all processes,
shared pages counted per process) once the accessibility tree is on.

## Switches and files

| Setting (`~/.config/voxtype-tui/features.json`) | Default |
|---|---|
| `learning.enabled` — learn and apply learned words | on |
| `learning.everywhere` — the managed accessibility files | off |
| `learning.notify` — the "Vocabulary learned" notification | on |
| `learning.keep_context` — store ~60 characters around each fix | off |

Turning learning off stops both learning and applying; learned entries stay,
paused. Reset deletes all of them.

| File | Content |
|---|---|
| `~/.config/voxtype-tui/learned.json` | learned entries (0600) |
| `~/.config/voxtype-tui/learned.apply.json` | compiled copy read on every dictation (derived; stamped with the generation of `learned.json` and ignored when it does not match, e.g. after a failed write) |
| `~/.config/voxtype-tui/learn-everywhere.json` | which managed edits are ours |
| `$XDG_RUNTIME_DIR/voxtype-tui/learn-watch.log` | watcher events, no text (RAM) |

None of them are in `sync.json`. Debugging with text in the log:
`VOXTYPE_TUI_LEARN_DEBUG=1`.

## Cost

- Post-processing: with a typical handful of learned words the stage takes
  about 0.2 ms per dictation (measured in an Omarchy VM on a real dictation).
  Worst case measured: 500 learned entries, 60 words, a fresh process each
  time (as Voxtype runs it): import + apply p95 2.5 ms. It never loads the
  50k-word list.
- The watcher exists only after a dictation, for at most 30 s (plus up to
  10 s for the Undo click), and costs ~0.2 % of one core and ~32 MB while
  armed. Nothing runs while you are not dictating.

## Headless API (`voxtype_tui.learned`, stdlib only)

```python
from voxtype_tui import learned, learn_everywhere, learn_watch

learned.submit_correction(record_or_id, edited_text, source="fix", kind=None)
#   -> {"ok", "candidates": [{heard, written, kind}], "applied": [entry],
#       "skipped": [{heard, written, reason}], "reason"?}
learned.list_entries()      # {"enabled", "everywhere", "entries", "counts"}
learned.accept(id) / reject(id) / forget(id) / undo(id) / reset()
learned.promote(id, add_to_user=None)   # -> {"ok", "entry", "added": {vocab, rules}}
learned.apply(text, protected_spans=(), user_phrases=())  # -> (text, hit_ids); never raises
learned.record_hits(hit_ids)             # off the critical path

learn_everywhere.preview(enable=None)   # {"files": [{path, action, diff}], "needs_relogin"}
learn_everywhere.set_enabled(bool)

learn_watch.on_post_output(record, window=None)  # post-output hook (already detached); window = delivery window
python3 -m voxtype_tui.learn_watch --text "…" -v   # run one watch by hand
```

Entries look like `{"id", "kind", "written", "heard": [...], "aliases": [...],
"state": "suggested|active|dormant|rejected|promoted", "source", "count",
"hits", "first_seen", "last_seen", "app"}`.
