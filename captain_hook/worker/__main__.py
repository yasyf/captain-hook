from __future__ import annotations

import importlib.metadata
import os
import sys

from loguru import logger

from captain_hook.daemon.context import ContextIO
from captain_hook.worker.runtime import ProductRuntime
from captain_hook.worker.service import WorkerService


def adopt_user_path() -> None:
    """Replace launchd's ``PATH`` with the user's own before anything discovers a command.

    A worker inherits ``/usr/bin:/bin:/usr/sbin:/sbin`` from the daemon, which hides every CLI the
    product resolves — ``claude`` for the plugin roster, ``claude``/``codex`` for the reviewer's
    judge — so it asks the user's login shell once instead. A probe that fails is recorded as a
    fault: the alternative is the state this fixes, where a backend nobody can find looks exactly
    like a machine with no backend installed.
    """
    from captain_hook import faults
    from captain_hook.util.userpath import LoginShellError, login_path, merged_path

    try:
        login = login_path()
    except LoginShellError as exc:
        logger.opt(exception=True).error("login shell PATH probe failed; user-installed CLIs stay invisible")
        faults.record("login shell PATH probe", exc)
        return
    os.environ["PATH"] = merged_path(os.environ.get("PATH", ""), login)


def main() -> None:
    adopt_user_path()
    protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    fallback = sys.stderr
    sys.stdout = ContextIO("stdout", fallback)
    sys.stderr = ContextIO("stderr", fallback)
    runtime = ProductRuntime()
    try:
        WorkerService(
            sys.stdin.buffer,
            protocol_output,
            build=importlib.metadata.version("capt-hook"),
            dispatch=runtime.dispatch,
        ).run()
    finally:
        runtime.close()
        protocol_output.close()


if __name__ == "__main__":
    main()
