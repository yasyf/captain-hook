from __future__ import annotations

import hashlib
import importlib.metadata
import os
import sys

from loguru import logger

from captain_hook.daemon.context import ContextIO
from captain_hook.daemon.logsink import configure_daemon_logging
from captain_hook.worker.runtime import ProductRuntime
from captain_hook.worker.service import WorkerService


def adopt_user_path() -> None:
    """Replace launchd's ``PATH`` with the user's own before anything discovers a command.

    A worker inherits ``/usr/bin:/bin:/usr/sbin:/sbin`` from the daemon, which hides every CLI the
    product resolves — ``claude`` for the plugin roster, ``claude``/``codex`` for the reviewer's
    judge — so it takes the user's own ``PATH`` instead, from cache where one is fresh. A probe
    that fails with nothing cached is recorded as a fault: the alternative is the state this
    fixes, where a backend nobody can find looks exactly like a machine with no backend installed.
    """
    from captain_hook import faults
    from captain_hook.util.userpath import LoginShellError, merged_path, user_path

    try:
        login = user_path()
    except LoginShellError as exc:
        logger.opt(exception=True).error("login shell PATH probe failed; user-installed CLIs stay invisible")
        faults.record("login shell PATH probe", exc)
        return
    os.environ["PATH"] = merged_path(os.environ.get("PATH", ""), login)


def worker_log_key(build: str) -> str:
    """The daemon-log key for this worker: the build plus a digest of the project root.

    The host runs one worker per project root and sets that root as the worker's cwd, so
    concurrent same-build workers keyed on the build alone would rotate one shared log file —
    loguru's rotation is per-process, and one worker's rename strands the others' handles on
    the unlinked inode.

    A root deleted under a live session leaves the worker with an unresolvable cwd; the pid
    stands in for it, keeping such workers on separate log files rather than killing dispatch.
    """
    try:
        root = os.path.realpath(os.getcwd())
    except FileNotFoundError:
        root = f"deleted-root-{os.getpid()}"
    return f"{build}-{hashlib.sha256(root.encode('utf-8', 'surrogatepass')).hexdigest()[:16]}"


def main() -> None:
    build = importlib.metadata.version("capt-hook")
    router = configure_daemon_logging(worker_log_key(build))
    adopt_user_path()
    protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    fallback = sys.stderr
    sys.stdout = ContextIO("stdout", fallback)
    sys.stderr = ContextIO("stderr", fallback)
    runtime = ProductRuntime()
    try:
        WorkerService(sys.stdin.buffer, protocol_output, build=build, dispatch=runtime.dispatch).run()
    finally:
        router.close()
        runtime.close()
        protocol_output.close()


if __name__ == "__main__":
    main()
