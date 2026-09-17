from __future__ import annotations

import importlib.metadata
import io
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from captain_hook import app
from captain_hook.daemon.context import ContextIO, bound_buffers
from captain_hook.util import reqenv
from captain_hook.worker.protocol import EventRequest
from captain_hook.worker.runtime import ProductRuntime
from tests.test_worker_protocol import frame, hello


@dataclass(slots=True)
class Snapshot:
    state: app.State
    discovery_stdout: str = "discovered out\n"
    discovery_stderr: str = "discovered err\n"


class FakeRegistry:
    def __init__(self) -> None:
        self.calls = 0

    def get(self) -> Snapshot:
        self.calls += 1
        return Snapshot(app.State())


def request(*, request_id: int = 1, event: str = "PreToolUse", payload_raw: str = "{}") -> EventRequest:
    return EventRequest(
        id=request_id,
        event=event,
        root="/project",
        cwd="/project/subdir",
        env={"CLAUDE_PROJECT_DIR": "/project"},
        payload_raw=payload_raw,
        client_pid=100,
        client_ppid=99,
        deadline_unix_ms=1_700_000_000_000,
    )


def test_dispatch_binds_request_scope_and_replays_cached_discovery() -> None:
    registry = FakeRegistry()
    seen: dict[str, Any] = {}

    def transcript_loader(_: object) -> None:
        return None

    def background() -> None:
        seen["background"] = reqenv.current()
        seen["background_buffers"] = bound_buffers()

    def dispatch(root: object, event: object, raw: object, **kwargs: object) -> tuple[dict[str, str], object]:
        seen.update(
            root=root,
            event=event,
            raw=raw,
            kwargs=kwargs,
            overrides=reqenv.current(),
            request_buffers=bound_buffers(),
        )
        return {"decision": "allow"}, background

    runtime = ProductRuntime(
        registry_factory=lambda _: registry,
        dispatcher=dispatch,
        transcript_loader=transcript_loader,
        install_writer=False,
        nlp_warmer=lambda: None,
    )
    response, after = runtime.dispatch(request())

    assert response.status == "ok"
    assert response.exit == 0
    assert response.stdout == 'discovered out\n{"decision": "allow"}\n'
    assert response.stderr == "discovered err\n"
    assert str(seen["root"]) == "/project"
    assert seen["event"].name == "PreToolUse"
    assert seen["kwargs"] == {
        "session_dir": None,
        "transcript_loader": transcript_loader,
    }
    assert seen["overrides"].cwd == "/project/subdir"
    assert seen["overrides"].client_ppid == 99
    assert seen["overrides"].deadline_unix_ms == 1_700_000_000_000
    assert reqenv.current() is None
    assert after is not None
    after()
    assert seen["background"].deadline_unix_ms == 1_700_000_000_000
    assert seen["background_buffers"] is not None
    assert seen["background_buffers"] is not seen["request_buffers"]
    assert reqenv.current() is None


def test_nlp_warms_once_after_the_first_reply_off_the_request_thread() -> None:
    release = threading.Event()
    warmed: list[str] = []

    def warmer() -> None:
        release.wait()
        warmed.append(threading.current_thread().name)

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(),
        dispatcher=lambda *_, **__: (None, lambda: None),
        install_writer=False,
        nlp_warmer=warmer,
    )
    _, first = runtime.dispatch(request(request_id=1))
    assert not any(t.name == "capt-hook-nlp-warm" for t in threading.enumerate())

    assert first is not None
    first()
    _, second = runtime.dispatch(request(request_id=2))
    assert second is not None
    second()
    assert warmed == []

    release.set()
    next(t for t in threading.enumerate() if t.name == "capt-hook-nlp-warm").join()
    assert warmed == ["capt-hook-nlp-warm"]


def test_registry_is_reused_for_the_same_root() -> None:
    registry = FakeRegistry()
    factories = 0

    def factory(_: object) -> FakeRegistry:
        nonlocal factories
        factories += 1
        return registry

    runtime = ProductRuntime(
        registry_factory=factory,
        dispatcher=lambda *_, **__: (None, lambda: None),
        install_writer=False,
        nlp_warmer=lambda: None,
    )
    runtime.dispatch(request(request_id=1))
    runtime.dispatch(request(request_id=2))

    assert factories == 1
    assert registry.calls == 2


def test_invalid_event_is_a_result_error_without_dispatch() -> None:
    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(),
        dispatcher=lambda *_, **__: (None, lambda: None),
        install_writer=False,
        nlp_warmer=lambda: None,
    )
    response, after = runtime.dispatch(request(event="NoSuchEvent"))

    assert response.status == "ok"
    assert response.exit == 1
    assert "Invalid event type: 'NoSuchEvent'" in response.stderr
    assert after is None


def test_dispatch_exception_returns_traceback_error() -> None:
    def fail(*_: object, **__: object) -> tuple[None, object]:
        raise ValueError("broken hook")

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(), dispatcher=fail, install_writer=False, nlp_warmer=lambda: None
    )
    response, after = runtime.dispatch(request())

    assert after is None
    assert response.status == "error"
    assert response.exit == 1
    assert "ValueError: broken hook" in response.stderr


def test_hook_writes_are_captured_inside_the_product_response() -> None:
    def dispatch(*_: object, **__: object) -> tuple[None, object]:
        print("hook stdout")
        print("hook stderr", file=sys.stderr)
        return None, lambda: None

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(), dispatcher=dispatch, install_writer=False, nlp_warmer=lambda: None
    )
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = ContextIO("stdout", io.StringIO())
    sys.stderr = ContextIO("stderr", io.StringIO())
    try:
        response, _ = runtime.dispatch(request())
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr

    assert response.stdout == "discovered out\nhook stdout\n"
    assert response.stderr == "discovered err\nhook stderr\n"


def test_worker_entrypoint_installs_the_daemon_log_sinks(tmp_path: Path) -> None:
    """PIN: ``capt-hook logs`` reads files only ``configure_daemon_logging`` writes.

    d7d554d deleted the Python daemon that called it, and the worker that took over dispatch
    never did — ``request_scope`` kept binding ``session_log_path`` with no sink consuming it,
    so every per-session log stopped being written on 2026-07-21.
    """
    import subprocess

    from captain_hook.worker.__main__ import worker_log_key

    logs = tmp_path / "logs"
    worker = subprocess.run(
        [sys.executable, "-m", "captain_hook.worker"],
        input=frame(hello(importlib.metadata.version("capt-hook"))),
        capture_output=True,
        env={**os.environ, "CAPTAIN_HOOK_LOG_DIR": str(logs)},
        timeout=180,
    )

    assert worker.returncode == 0, worker.stderr.decode()
    assert (logs / f"daemon-{worker_log_key(importlib.metadata.version('capt-hook'))}.log").exists()


def test_workers_in_different_roots_write_different_daemon_logs(tmp_path: Path) -> None:
    """PIN: the host runs one worker per project root, all of the same build, concurrently.

    A log keyed on the build alone made them share one file with independent loguru rotation
    state — one process's rotation rename silently drops the others' lines into the unlinked
    inode — so the key carries a digest of the root the host set as the worker's cwd.
    """
    import os
    import subprocess

    logs = tmp_path / "logs"
    for root in (tmp_path / "a", tmp_path / "b"):
        root.mkdir()
        worker = subprocess.run(
            [sys.executable, "-m", "captain_hook.worker"],
            input=frame(hello(importlib.metadata.version("capt-hook"))),
            capture_output=True,
            cwd=root,
            env={**os.environ, "CAPTAIN_HOOK_LOG_DIR": str(logs)},
            timeout=180,
        )
        assert worker.returncode == 0, worker.stderr.decode()

    assert len(list(logs.glob("daemon-*.log"))) == 2


def test_worker_survives_a_root_deleted_under_it(tmp_path: Path) -> None:
    """PIN: an Orca workspace deleted under a live session leaves the worker with no cwd.

    ``worker_log_key`` resolved the root through ``os.getcwd()``, which raises
    ``FileNotFoundError`` once that directory is gone, so every dispatch from such a session
    killed its worker before ``WorkerService`` ever ran — observed 2026-09-02 crash-looping
    against ``~/.orca/workspaces/monorepo-old``, which took the host's socket down with it.
    """
    import os
    import subprocess

    root = tmp_path / "gone"
    root.mkdir()
    logs = tmp_path / "logs"
    worker = subprocess.run(
        ["/bin/sh", "-c", 'cd "$1" && rmdir "$1" && exec "$0" -m captain_hook.worker', sys.executable, str(root)],
        input=frame(hello(importlib.metadata.version("capt-hook"))),
        capture_output=True,
        env={**os.environ, "CAPTAIN_HOOK_LOG_DIR": str(logs)},
        timeout=180,
    )

    assert worker.returncode == 0, worker.stderr.decode()
    assert len(list(logs.glob("daemon-*.log"))) == 1


def test_worker_bounds_the_transcript_parse_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.worker.__main__ import TRANSCRIPT_PARSE_THREADS, bound_transcript_parse_pool

    monkeypatch.delenv("CC_TRANSCRIPT_PARSE_THREADS", raising=False)
    bound_transcript_parse_pool()
    assert os.environ["CC_TRANSCRIPT_PARSE_THREADS"] == str(TRANSCRIPT_PARSE_THREADS)

    monkeypatch.setenv("CC_TRANSCRIPT_PARSE_THREADS", "2")
    bound_transcript_parse_pool()
    assert os.environ["CC_TRANSCRIPT_PARSE_THREADS"] == "2"
