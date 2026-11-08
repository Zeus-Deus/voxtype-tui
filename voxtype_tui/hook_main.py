"""``voxtype-tui-hook <event>`` entry point, kept lean on purpose.

Voxtype runs ``pre_recording_command`` and waits for it (capture is not
blocked, measured on 1.1.1), and waits for ``post_output_command`` before it
goes idle. So this module imports nothing but ``os``/``sys`` before it
double-forks: the hook returns after interpreter start-up + one fork, and the
real work (:mod:`voxtype_tui.hooks`) runs in a detached process whose stdio is
``/dev/null`` (voxtype never waits on it). Any failure exits 0.
"""
import os
import sys

_DETACHED_EVENTS = ("pre-recording", "post-output")


def _detach() -> bool:
    """Double-fork. True in the detached grandchild, False in the caller."""
    pid = os.fork()
    if pid > 0:
        os.waitpid(pid, 0)
        return False
    try:
        os.setsid()
        if os.fork() > 0:
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        # Voxtype waits on the hook's stdio pipes: never hold them open.
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        if devnull > 2:
            os.close(devnull)
        return True
    except BaseException:
        os._exit(0)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    import time

    # The moment voxtype ran this hook, before any fork or delay: the
    # detached worker uses it to tell this dictation from a newer one.
    os.environ["VOXTYPE_TUI_HOOK_STARTED"] = repr(time.time())
    if event == "pre-output":
        return 0
    if event == "post-output":
        # Bind the worker to this dictation's record now, while voxtype
        # waits: a worker that starts late must never pick up a newer one.
        try:
            from . import rtdir

            os.environ["VOXTYPE_TUI_HOOK_RECORD"] = rtdir.claim_newest_handoff()
        except BaseException:
            os.environ["VOXTYPE_TUI_HOOK_RECORD"] = ""
    if event in _DETACHED_EVENTS:
        try:
            if not _detach():
                return 0
        except BaseException:
            return 0
        try:
            from . import hooks

            hooks.main([event])
        except BaseException:
            pass
        finally:
            os._exit(0)
    from . import hooks

    return hooks.main(argv)


if __name__ == "__main__":
    sys.exit(main())
