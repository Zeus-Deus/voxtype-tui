"""In-app sudo password prompt + runner.

Provides `SudoPasswordModal` — a Textual ModalScreen themed to match the
rest of the app — and `run_sudo_command`, which pipes the password to
`sudo -S` via stdin.

Security posture:
  * The password is NEVER placed in argv. `sudo -S -p ''` reads from stdin,
    which keeps the password out of `/proc/<pid>/cmdline` and shell history
    for any process inspector / audit log.
  * The prompt suffix (`-p ''`) suppresses sudo's own prompt so nothing we
    might capture contains the password or a re-prompt.
  * We never write the password to the RichLog or any other UI element;
    only sudo's stdout/stderr are surfaced, and we strip sudo's
    "[sudo] password for ..." lines if they ever appear.
  * Python strings are immutable and GC'd eventually — we can't zero the
    buffer the way C code can. Callers should discard the password as
    soon as the sudo run returns. This is the same tradeoff polkit,
    ksshaskpass, etc. accept.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from .theme import MODAL_BORDER_STYLE


@dataclass(frozen=True)
class SudoResult:
    """Return value of `run_sudo_command`. `ok` means exit code 0.
    `incorrect_password` is True when sudo rejected the password (exit 1
    with the canonical auth-failure stderr message). `output` is the
    combined stdout+stderr with any `[sudo]`-prompt lines filtered."""

    ok: bool
    returncode: int
    output: str
    incorrect_password: bool = False


_SUDO_PROMPT_MARKERS: tuple[str, ...] = (
    "[sudo] password",
    "Sorry, try again",
    "1 incorrect password attempt",
)


def _filter_sudo_noise(text: str) -> str:
    """Strip sudo's own prompt and attempt-counter lines from captured
    output. These never contain the password — they would only confuse the
    user since we handle auth in-modal — but we drop them for cleanliness."""
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if any(marker in stripped for marker in _SUDO_PROMPT_MARKERS):
            continue
        kept.append(line)
    return "\n".join(kept)


def run_sudo_command(
    argv: Sequence[str],
    password: str,
    timeout: float = 30.0,
) -> SudoResult:
    """Run `sudo -S -p '' <argv...>` with `password` piped to stdin.

    `argv` is the command as it would be typed after `sudo` — e.g.
    `["voxtype", "setup", "gpu", "--enable"]`. We build the full command
    here to keep the call sites simple and to guarantee we always pass
    `-S -p ''`.

    Returns a `SudoResult`. We never raise on non-zero exit; callers decide
    how to surface failures."""
    if not argv:
        return SudoResult(ok=False, returncode=-1, output="empty command", incorrect_password=False)

    full = ["sudo", "-S", "-p", "", "--", *argv]
    try:
        proc = subprocess.run(
            full,
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return SudoResult(ok=False, returncode=-1, output="sudo binary not found", incorrect_password=False)
    except subprocess.TimeoutExpired:
        return SudoResult(ok=False, returncode=-1, output=f"sudo timed out after {timeout:.0f}s", incorrect_password=False)

    stderr = proc.stderr or ""
    stdout = proc.stdout or ""
    combined = _filter_sudo_noise(
        (stdout + ("\n" if stdout and stderr else "") + stderr).rstrip()
    )
    bad_pw = (
        proc.returncode != 0
        and ("incorrect password" in stderr.lower() or "sorry, try again" in stderr.lower())
    )
    return SudoResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        output=combined,
        incorrect_password=bad_pw,
    )


class SudoPasswordModal(ModalScreen[str | None]):
    """Password prompt. Dismisses with the entered password on submit or
    `None` on cancel. Callers handle actually running sudo — this modal
    only collects credentials."""

    DEFAULT_CSS = f"""
    SudoPasswordModal {{ align: center middle; }}
    SudoPasswordModal > Vertical {{
        background: $panel;
        border: {MODAL_BORDER_STYLE} $accent;
        padding: 1 2;
        width: 60;
        height: auto;
    }}
    SudoPasswordModal #title {{ text-style: bold; color: $accent; margin-bottom: 1; }}
    SudoPasswordModal #action {{ color: $text-muted; margin-bottom: 1; }}
    SudoPasswordModal #error {{ color: $error; margin-bottom: 1; height: auto; }}
    SudoPasswordModal #error.-hidden {{ display: none; }}
    SudoPasswordModal Input {{ margin-bottom: 1; }}
    SudoPasswordModal Horizontal {{ height: auto; align: center middle; }}
    SudoPasswordModal Button {{ margin: 0 1; }}
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        action_label: str,
        *,
        title: str = "Authentication required",
        initial_error: str | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._action_label = action_label
        self._initial_error = initial_error

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title, id="title")
            yield Label(self._action_label, id="action")
            yield Label(
                self._initial_error or "",
                id="error",
                classes="" if self._initial_error else "-hidden",
            )
            yield Input(placeholder="sudo password", password=True, id="sudo-password")
            with Horizontal():
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", variant="default", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#sudo-password", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        elif event.button.id == "cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        value = self.query_one("#sudo-password", Input).value
        # Empty submit is treated as a no-op so users can't accidentally
        # send an empty password to sudo (which would always fail and also
        # make retry UX confusing).
        if not value:
            return
        self.dismiss(value)
