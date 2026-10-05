from __future__ import annotations

import hashlib
import importlib.metadata
import json
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
    service = WorkerService(sys.stdin.buffer, protocol_output, dispatch=runtime.dispatch, guarded=runtime.guarded)
    pipeline._ADOPTER = service.adopt
    try:
        service.run()
    finally:
        router.close()
        runtime.close()
        protocol_output.close()


def evaluate() -> None:
    """Answer one host event request read from stdin with this build's runtime, for a client whose host is unreachable.

    The request is the exact body the client would have sent the host, and the reply is the
    ``EventResponse`` a host worker returns, so the client grades the guard's completion the same way.
    Background work is dropped: the reply is the whole job.
    """
    from captain_hook.worker.protocol import OP_EVENT, PROTOCOL, decode_event

    request = decode_event({"protocol": PROTOCOL, "op": OP_EVENT, "id": 1, "request": json.load(sys.stdin)})
    reply = os.fdopen(os.dup(sys.stdout.fileno()), "w")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    from captain_hook.daemon.context import install_context_io
    from captain_hook.snapshots.client import client_scope
    from captain_hook.worker.runtime import ProductRuntime

    bound_transcript_parse_pool()
    skip_bundled_cli_version_probe()
    install_context_io()
    runtime = ProductRuntime(install_writer=False)
    try:
        with client_scope():
            response, _ = runtime.dispatch(request)
    finally:
        runtime.close()
    with reply:
        json.dump(response.message(), reply)


def evaluate_actor(provider: str) -> None:
    """Answer one event for a remote API-key actor, then finish its background hooks before exiting.

    The actor's API keys move from this process's environment into the judge it calls, so no
    hook command or git child inherits them, and the reply is the same ``EventResponse`` a host
    worker returns.
    """
    from captain_hook import actor

    actor.capture(provider)
    from captain_hook.worker.protocol import OP_EVENT, PROTOCOL, decode_event

    request = decode_event({"protocol": PROTOCOL, "op": OP_EVENT, "id": 1, "request": json.load(sys.stdin)})
    if request.deadline_unix_ms <= 0:
        raise SystemExit("capt-hook: an actor's event request carries no deadline")
    from captain_hook.log import setup_logging

    setup_logging(json.loads(request.payload_raw).get("session_id"))
    reply = os.fdopen(os.dup(sys.stdout.fileno()), "w")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    from captain_hook.daemon.context import install_context_io
    from captain_hook.snapshots.client import client_scope
    from captain_hook.worker.runtime import ProductRuntime
    from captain_hook.worker.service import BACKGROUND_SNAPSHOT_CLIENT

    bound_transcript_parse_pool()
    skip_bundled_cli_version_probe()
    install_context_io()
    runtime = ProductRuntime(install_writer=False, nlp_warmer=lambda: None)
    try:
        with client_scope() as client:
            token = BACKGROUND_SNAPSHOT_CLIENT.set(client)
            try:
                response, background = runtime.dispatch(request)
                with reply:
                    json.dump(response.message(), reply)
                if background is not None:
                    background()
            finally:
                BACKGROUND_SNAPSHOT_CLIENT.reset(token)
    finally:
        runtime.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["evaluate"]:
        evaluate()
    elif sys.argv[1:2] == ["evaluate-actor"] and len(sys.argv) == 3:
        evaluate_actor(sys.argv[2])
    else:
        main()
