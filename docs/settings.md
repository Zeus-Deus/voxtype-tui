# Settings

Eight collapsible sections. Sections with ◎ require a daemon restart when their fields change (the TUI prompts after save); everything else takes effect on the next transcription.

## Engine & Model ◎

**Engine** — which speech-to-text backend Voxtype uses.

| Engine | Best for | Model files |
|---|---|---|
| `whisper` | General-purpose, multilingual; the default | `ggml-*.bin` |
| `parakeet` | English only, very fast on NVIDIA GPUs | NeMo checkpoints |
| `moonshine` | Small/fast, English | ONNX |
| `sensevoice`, `paraformer`, `dolphin`, `omnilingual` | Language-specific backends | Varies |

Changing engine rebuilds the Model dropdown with that engine's catalog. Each engine stores its model at a different TOML path (`whisper.model`, `parakeet.model_type`, `moonshine.model`, …). Flipping engines **does not** delete the old engine's model setting — you can toggle back later without losing your pick.

**Model** — which trained weights to use. Whisper sizes run from `tiny` (~75MB, fast, accuracy-limited) to `large-v3` (~3GB, slow, best accuracy). `large-v3-turbo` is a smaller/faster variant of large-v3 — usually the right choice on any machine with GPU support. `.en` variants are English-only, faster, and a bit more accurate for English than the multilingual equivalents.

Pick `Custom path…` to point at a local `.bin` file Voxtype would otherwise not know about.

**Language** — ISO 639-1 code (`en`, `de`, `fr`, …) or `auto` for auto-detect. Only applies to `whisper`.

## Hotkey & Activation ◎

**Key** — the key you hold to record. Accepts evdev names: `SCROLLLOCK`, `PAUSE`, `RIGHTALT`, `F13`–`F24`, etc. Run `evtest` to see what name your keyboard sends for a given key.

**Modifiers** — optional keys that must also be held. Four checkboxes: `LEFTCTRL`, `LEFTALT`, `LEFTSHIFT`, `LEFTMETA`. Right-variants aren't offered here — hand-edit the TOML if you need one.

**Mode** — how the key behaves:

- `push_to_talk` — record while held, transcribe on release.
- `toggle` — first press starts recording, second press stops and transcribes.

**Built-in detect** — whether voxtype's own evdev hotkey listener runs. Turn this **off** if you bind the hotkey in your window manager instead (Hyprland, Sway). Omarchy does this by default: `Super + Ctrl + X` is bound to `voxtype record toggle`, so the daemon's built-in detection is redundant.

## Audio ◎

**Device** — which microphone to record from. The dropdown is populated from `pactl list sources short` (filtered to real `alsa_input.*` devices, monitor-sinks excluded). `default` means the system default. `Custom path…` lets you type an exact name if pactl isn't enumerating what you want.

**Max duration** — safety cap on a single recording in seconds (default 60). Not restart-sensitive — change anytime.

**Feedback sounds** — beeps when recording starts/stops. Enable + pick a theme (`default`, `subtle`, `mechanical`) + volume (`0.0`–`1.0`).

## Output

None of these need a restart.

**Mode** —

- `type` — simulates keystrokes at the cursor (needs `ydotool` or `wtype`).
- `clipboard` — copies to clipboard only (needs `wl-copy`).
- `paste` — copies then `Ctrl+V`.

**Fallback to clipboard** — if typing fails (app doesn't accept synthetic events, etc.), fall through to clipboard mode silently.

**Auto-submit** — presses Enter after outputting. Useful when dictating into chat apps, shell prompts, form fields.

**Smart auto-submit** — say the word "submit" at the end of your sentence, and voxtype presses Enter for you (the word itself is stripped from the output). More conversational than leaving auto-submit on all the time.

**Spoken punctuation** — say "period", "comma", "question mark", "exclamation mark", etc. and get the literal character. Off by default because it mangles natural speech that uses those words literally.

**Type delay (ms)** — pause between each simulated keystroke. Usually 0. Increase if the receiving app drops characters at high input rates.

## GPU acceleration

Shows the current backend (CPU AVX2 / AVX-512 / Vulkan / CUDA / ROCm) and any detected GPUs. Enable/Disable are system-level changes — they need `sudo` to swap the `/usr/bin/voxtype` symlink, so the buttons pop a floating terminal with `sudo voxtype setup gpu --enable` (or `--disable`). After the terminal closes, hit **Refresh status**.

When to enable:

- NVIDIA GPU → `voxtype setup gpu --enable` picks CUDA for Parakeet, Vulkan for Whisper. Huge latency win on large models.
- AMD GPU → ROCm or Vulkan.
- Intel GPU / no dedicated GPU → Vulkan may still help for Whisper; often comparable to AVX-512 CPU.

## VAD (Voice Activity Detection) ◎

Collapsed by default. When enabled, voxtype trims silence from the ends of your recording and chunks continuous speech. Improves accuracy on longer recordings at the cost of a small preprocessing step.

**Enable** — toggle. **Model** — path to a Silero VAD `.onnx` (leave empty for voxtype's bundled default). **Threshold** — 0.0 to 1.0, how loud counts as speech (default 0.5; raise for noisy rooms).

## Post-processing

Collapsed by default. Pipe the transcribed text through an external command before it reaches your cursor. Classic use case: clean up filler words with a local LLM.

```toml
[output.post_process]
command = "ollama run llama3.2:1b 'Clean up this dictation. Output only the cleaned text:'"
timeout_ms = 30000
```

The command receives text on stdin and must write the cleaned text to stdout. On timeout or non-zero exit, voxtype falls back to the original transcription — safe by default.

## Remote backend ◎

Collapsed by default. Run Whisper on another machine (homelab server, cloud) and stream audio there. Voxtype supports two shapes: a self-hosted `whisper.cpp` server or any OpenAI-compatible API.

- **Endpoint** — `http://192.168.1.42:8080` for self-hosted, `https://api.openai.com` for OpenAI.
- **Model name** — `whisper-1` for OpenAI; whatever the server expects for self-hosted.
- **API key** — password-masked Input. Empty = use the `VOXTYPE_WHISPER_API_KEY` env var instead (safer for shell-scripted setups).
- **Timeout** — seconds before the remote call is abandoned.

Only applies when `[whisper].mode = "remote"` (set in the Engine section or hand-edit).
