from __future__ import annotations

import hashlib
import importlib.metadata
import os
import resource
import sys

from captain_hook.worker.service import WorkerService, handshake

TRANSCRIPT_PARSE_THREADS = 4


def bound_transcript_parse_pool() -> None:
    os.environ.setdefault("CC_TRANSCRIPT_PARSE_THREADS", str(TRANSCRIPT_PARSE_THREADS))


def skip_bundled_cli_version_probe() -> None:
    """Stop the Claude Agent SDK from spawning ``claude -v`` before every LLM call.

    The SDK checks the version of the CLI it bundles, which is pinned with it, so the probe can
    only ever pass; on a machine near its process limit it is one more spawn per hook LLM call.
    """
    os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")


def lift_spawn_nproc_cap() -> None:
    """Raise the ``RLIMIT_NPROC`` soft limit daemonkit lowered across this worker's spawn back to the hard limit.

    daemonkit caps a spawned child at the user's process count at spawn plus 400, for the child's whole
    life. A worker that outlives a busy hour then fails every ``ps``, ``git``, and ``claude`` spawn with
    ``EAGAIN``, and a guard that must read the process table fails closed on every call.
    """
    _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    resource.setrlimit(resource.RLIMIT_NPROC, (hard, hard))


def adopt_user_path() -> None:
    """Replace launchd's ``PATH`` with the user's own before anything discovers a command.

    A worker inherits ``/usr/bin:/bin:/usr/sbin:/sbin`` from the daemon, which hides every CLI the
    product resolves — ``claude``/``codex`` for the reviewer's judge — so it takes the user's own
    ``PATH`` instead, from cache where one is fresh. A probe that fails with nothing cached is
    recorded as a fault: the alternative is the state this fixes, where a backend nobody can find
    looks exactly like a machine with no backend installed.
    """
    from loguru import logger

    from captain_hook import faults
    from captain_hook.util.userpath import LoginShellError, merged_path, user_path

    try:
        login = user_path()
    except LoginShellError as exc:
        logger.opt(exception=True).error("login shell PATH probe failed; user-installed CLIs stay invisible")
        faults.record("login shell PATH probe", exc)
        return
    os.environ["PATH"] = merged_path(os.environ.get("PATH", ""), login)


def worker_log_key(build: str, shard: str) -> str:
    try:
        root = os.path.realpath(os.getcwd())
    except FileNotFoundError:
        root = f"deleted-root-{os.getpid()}"
    return f"{build}-{hashlib.sha256(root.encode('utf-8', 'surrogatepass')).hexdigest()[:16]}-{shard}"


def main() -> None:
    build = importlib.metadata.version("capt-hook")
    protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    if not handshake(sys.stdin.buffer, protocol_output, build=build):
        protocol_output.close()
        return
    from captain_hook.daemon.context import ContextIO
    from captain_hook.daemon.logsink import configure_daemon_logging

    lift_spawn_nproc_cap()
    bound_transcript_parse_pool()
    skip_bundled_cli_version_probe()
    router = configure_daemon_logging(worker_log_key(build, os.environ["CAPT_HOOK_WORKER_SHARD"]))
    adopt_user_path()
    fallback = sys.stderr
    sys.stdout = ContextIO("stdout", fallback)
    sys.stderr = ContextIO("stderr", fallback)
    from captain_hook.review import pipeline
    from captain_hook.worker.runtime import ProductRuntime

    runtime = ProductRuntime()
    service = WorkerService(sys.stdin.buffer, protocol_output, dispatch=runtime.dispatch)
    pipeline._ADOPTER = service.adopt
    try:
        service.run()
    finally:
        router.close()
        runtime.close()
        protocol_output.close()


if __name__ == "__main__":
    main()
