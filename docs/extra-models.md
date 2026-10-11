# Extra models: quantized whisper + Qwen3-ASR

voxtype's own downloader (`voxtype setup --download`) only knows its built-in
model names. voxtype-tui adds a small catalog of **extra** models and
downloads them itself with `voxtype_tui/downloader.py`: HTTPS only, every file
pinned by sha256 and size, written to `<file>.part`, verified, then renamed
atomically. Staging entries must be singly-linked regular files, opened with
`O_NOFOLLOW`; symlinks, hardlinks and nonregular entries are refused without
touching the destination. Cancellation removes only the owned safe `.part`
file; unsafe or replaced staging entries are left alone. In the UI these
models sit next to the native ones and download, delete and set-active the
same way.

The logic lives in two modules that don't import Textual,
`voxtype_tui/extra_models.py` and `voxtype_tui/asr_server.py`.
`models.py` and `settings.py` only render what those modules return.

## Quantized whisper (`large-v3-turbo-q5_0`, `large-v3-turbo-q8_0`)

- File: `ggml-<name>.bin` in voxtype's models dir, so the existing
  scan/size/delete code works unchanged.
- Config value: voxtype rejects the catalog name but looks up any bare
  filename ending in `.bin` inside its models dir. The config gets
  `model = "ggml-<name>.bin"` (`config_model_value`), and every reader maps it
  back to the catalog name (`catalog_model_name`).
- These only work with the GPU build: quantized ggml crashes voxtype's CPU
  binaries. They're offered only when `voxtype info variants --json` reports
  the Vulkan binary. On other machines they stay visible but can't be
  downloaded or selected, with the reason "needs GPU acceleration". If a
  quantized model is active and voxtype isn't running the Vulkan build, a
  warning shows at load time and after a GPU enable/disable.

## Qwen3-ASR 0.6B (engine `qwen3-asr`, shown as "Qwen3-ASR")

The catalog has two models: `0.6b-q8_0` (961 MB, best accuracy) and
`0.6b-q4_k` (602 MB, smaller and faster on CPU). voxtype can't run Qwen3
itself, so voxtype-tui runs it behind voxtype's whisper **remote** mode:

1. Downloading the model also fetches the pinned CrispASR v0.8.42 build into
   `$XDG_DATA_HOME/voxtype-tui/engines/crispasr-0.8.42-<variant>/`
   (sha256-verified, then extracted into a fresh empty dir with tarfile's
   `data` filter). The variant is `vulkan` when voxtype runs its Vulkan
   binary; otherwise `avx512` if the CPU has avx512f, `generic` (avx2), or
   `cpu-legacy`. The GGUF goes into voxtype's models dir as
   `qwen3-asr-0.6b-<quant>.gguf`.
2. Activating it writes the systemd user unit `voxtype-asr.service`:
   `crispasr --server --host 127.0.0.1 --port <port> --backend qwen3 -m <gguf>
   -l <lang> -t <physical cores> [-ng] --hotwords ${VOXTYPE_ASR_HOTWORDS}`
   with `PartOf=voxtype.service`, `Before=voxtype.service` and
   `[Install] WantedBy=voxtype.service`. Once enabled, `systemctl --user
   start|stop|restart voxtype` starts, stops or restarts the server too
   (verified on systemd 261). On the Vulkan build the unit also gets the
   `gpu.conf` device-pinning variables. The port is a free one picked at
   activation and stored in the sidecar.
3. voxtype's config gets `engine = "whisper"` plus `[whisper] mode =
   "remote"`, `remote_endpoint = "http://127.0.0.1:<port>"`,
   `remote_model = "qwen3-asr"`, `remote_timeout_secs = 60`. The previous
   whisper mode/remote settings are saved in the sidecar (`qwen3_asr` in
   `metadata.json`) and restored when you switch away. Qwen3-ASR only counts
   as active when the sidecar marker is set **and** the endpoint matches, so a
   remote server you configured yourself still shows as remote whisper.
4. Vocabulary: voxtype's remote client only sends `prompt`, which Qwen3
   ignores. The vocabulary goes to the server as `--hotwords` instead, read
   from `$XDG_CONFIG_HOME/voxtype-tui/qwen3-asr.env`, which is rewritten on
   every save. The restart voxtype already needs after a save also restarts
   the server, so the new hotwords take effect then.
5. Saving a switch away records pending teardown but keeps the helper running,
   enabled and linked to voxtype: the current daemon still caches its remote
   endpoint. After a **verified daemon restart**, the shared restart API
   disables/stops the obsolete helper and removes its unit and env file.
   A failed/no-op restart or failed cleanup retains the pending marker for a
   retry. Reopening the TUI restores the stale pill; reactivating Qwen3 cancels
   teardown. Deleting the active model, or Qwen3 files while teardown is
   pending, is refused. Deleting the last Qwen3 model after teardown also
   removes the CrispASR build.

`AppState.save` calls `asr_server.apply_after_save` once config.toml and
metadata.json are written. Only state using **both default config and sidecar
paths** manages the global helper; alternate-config/sidecar saves are file-only,
including the Settings GPU helper reconciliation path. The low-level
`apply_after_save(doc, sc, *, config_path=None, sidecar_path=None)` API explicitly
targets the default service when paths are omitted; callers with paths must
pass both. That function writes the unit and env file, runs `daemon-reload`,
then `enable`, or records `$XDG_CONFIG_HOME/voxtype-tui/qwen3-asr.teardown`.
It reports problems instead of raising. If Qwen3 was never used it doesn't
touch systemd. Setting
`VOXTYPE_TUI_SYSTEMCTL=0` turns every systemctl call into a no-op; the tests
set it.

## Headless API for the Omarchy plugin

Everything below imports without Textual (`voxtype_tui.extra_models`,
`.asr_server`, `.downloader`, `.state`). `models_dir` defaults to
`VOXTYPE_MODELS_DIR` if set, else `$XDG_DATA_HOME/voxtype/models`. Mutations
take a loaded `state.AppState`; the caller then runs `st.save()` and
`voxtype_tui.voxtype_cli.restart_daemon()` (or `restart_daemon_async()`). This
shared entrypoint completes pending teardown after checking the daemon's
PID/start timestamp changed. It is used by the TUI's Restart-now modal,
explicit restart binding, exit hook, and headless migration CLI. Explicit
restart still always requests a restart, even with fresh config; only the
automatic exit path is stale-gated. The modal appears on the first save with
restart-sensitive changes; **Later** keeps dictation on the old helper until
an explicit restart or exit-time restart. Raw external `systemctl restart`
still propagates through `PartOf`, but does not finish the frontend's pending
teardown; integrations must use the shared API. No recognition/model-loading
claims are implied by fixture-based systemd tests.

```python
from voxtype_tui import extra_models as xm, asr_server as asr

# catalog / availability
xm.ENGINE_QWEN3                       # "qwen3-asr" (UI engine key; never written to `engine =`)
xm.engine_label(engine) -> str        # "Qwen3-ASR"
xm.list_extra_models(engine=None, *, models_dir=None) -> list[dict]
    # {engine, engine_label, name, size_mb, downloaded, on_disk_bytes,
    #  available, disabled_reason, path, note}
xm.availability(engine, name) -> tuple[bool, str | None]      # natives -> (True, None)
xm.config_model_value(engine, name) -> str                    # catalog name -> config value
xm.catalog_model_name(engine, value) -> str                   # config value -> catalog name
xm.quantized_whisper_warning(doc) -> str | None               # GPU-only model without Vulkan

# download / cancel / delete
xm.download_argv(engine, name) -> list[str]
    # Child process that prints `NN.N%` frames like `voxtype setup --download`
    # (same parser: models.split_terminal_output / parse_percent). Exit 0 = ok,
    # SIGTERM -> exit 130 with the .part removed. Never edits config.toml.
    # Native models get voxtype's own argv.
xm.download(engine, name, *, models_dir=None, progress=None, cancel=None) -> pathlib.Path
    # in-process variant; progress(pct: float, label: str); cancel: threading.Event
xm.cleanup_partial(engine, name, models_dir=None) -> bool     # stale .part after a SIGKILL
xm.delete(engine, name, *, doc=None, sc=None, models_dir=None) -> int   # bytes freed
    # refuses while active; deleting the last Qwen3 model also removes the engine build

# active engine / model
asr.set_active_model(st, engine, name, *, models_dir=None, is_installed=None) -> dict
    # any engine including "qwen3-asr"; switching to or away from Qwen3
    # remembers / restores the whisper settings. Raises xm.RefusedError
    # (not downloaded, needs GPU, engine build missing).
asr.deactivate_qwen3(st, *, restore_engine=True) -> bool
asr.active_engine_model(doc, sc) -> tuple[str, str]           # (engine, catalog model name)
asr.is_qwen3_active(doc, sc) -> bool
asr.status(doc, sc, *, probe=True) -> dict
    # {engine, engine_label, model, qwen3: {active, model, port, endpoint, variant,
    #  unit: {active_state, main_pid, ...}, healthy, detail}}

# vocabulary
asr.regenerate_hotwords(sc) -> bool
    # st.save() already does this; call it only after saving the sidecar on its own
```

`doc` and `sc` can be the plain `tomllib` / `json` dicts the bridge's fast
status path already reads. `status()` costs one `systemctl show` plus one
`/health` GET (0.5 s timeout); pass `probe=False` to get just the
engine/model line.

Health requires the pinned CrispASR CLI server response
`{"status":"ok","backend":"qwen3"}` (additional keys are allowed), not
merely HTTP 200. This schema comes from v0.8.42 commit
`a53b513d1c54eba8a1bffa48b2cecb9e72f1ca35`,
`examples/cli/crispasr_server.cpp:2388-2395`; the CLI is started with
`--backend qwen3`. When systemd status is available, the managed unit must
also be active/running with a nonzero PID. A stopped unit with a listener on
its port reports **port taken by another program**, even if that listener
returns valid-looking health JSON. `detail` stays one short human line;
technical information goes in `reason`.
