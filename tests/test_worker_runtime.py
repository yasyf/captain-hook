from __future__ import annotations

import importlib.metadata
import io
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from captain_hook import app
from captain_hook.daemon.context import ContextIO, bound_buffers
from captain_hook.snapshots.client import AttachmentLimit, EvidenceIncomplete, GraphEvidenceExpired
from captain_hook.util import reqenv
from captain_hook.worker.protocol import EventRequest
from captain_hook.worker.runtime import ProductRuntime
from tests.test_worker_protocol import frame, hello


@dataclass(slots=True)
class Snapshot:
    state: app.State
    discovery_stdout: str = "discovered out\n"
    discovery_stderr: str = "discovered err\n"
    tools: dict = field(default_factory=dict)


class FakeRegistry:
    def __init__(self, state: app.State | None = None) -> None:
        self.calls = 0
        self.state = state or app.State()

    def get(self) -> Snapshot:
        self.calls += 1
        return Snapshot(self.state)


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


def test_dispatch_writes_a_plain_text_envelope_verbatim() -> None:
    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(),
        dispatcher=lambda *_, **__: ("Keep the plan path.", lambda: None),
        install_writer=False,
        nlp_warmer=lambda: None,
    )
    response, _ = runtime.dispatch(request(event="PreCompact"))

    assert response.stdout == "discovered out\nKeep the plan path.\n"


def test_dispatch_logs_one_line_with_latency_and_abandoned_hooks(logcap: Any) -> None:
    def dispatch(root: object, event: object, raw: object, **kwargs: object) -> tuple[None, object]:
        reqenv.abandoned().append("straggler")
        return None, lambda: None

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(),
        dispatcher=dispatch,
        install_writer=False,
        nlp_warmer=lambda: None,
    )
    runtime.dispatch(request(payload_raw='{"session_id": "sess"}'))
    runtime.dispatch(request(event="NotAnEvent"))

    served, rejected = [record.message for record in logcap.records if record.message.startswith("dispatch ")]
    assert "event='PreToolUse'" in served
    assert "root='/project'" in served
    assert "queue_ms=" in served
    assert "elapsed_ms=" in served
    assert "abandoned=['straggler']" in served
    assert "session_log_path" not in served
    assert "event='NotAnEvent'" in rejected
    assert "abandoned=[]" in rejected


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


@pytest.mark.parametrize("status", ["retained_limit", "lease_limit"])
def test_snapshot_capacity_failure_allows_hook_without_traceback(status: str) -> None:
    def fail(*_: object, **__: object) -> tuple[None, object]:
        raise EvidenceIncomplete(status, "snapshot capacity occupied")

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(), dispatcher=fail, install_writer=False, nlp_warmer=lambda: None
    )
    response, after = runtime.dispatch(request())

    assert after is None
    assert response.status == "ok"
    assert response.exit == 0
    assert response.stdout == ""
    assert response.stderr == ""


def test_attachment_bound_failure_allows_hook_without_traceback() -> None:
    def fail(*_: object, **__: object) -> tuple[None, object]:
        raise AttachmentLimit()

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(), dispatcher=fail, install_writer=False, nlp_warmer=lambda: None
    )
    response, after = runtime.dispatch(request(event="Stop"))

    assert after is None
    assert response.status == "ok"
    assert response.exit == 0
    assert response.stdout == ""
    assert response.stderr == ""


@pytest.mark.parametrize(
    "status",
    ["incomplete", "source_limit", "entry_limit", "output_limit", "deadline", "cancelled", "changed", "missing"],
)
def test_bounded_graph_evidence_fails_open_without_traceback(status: str) -> None:
    def fail(*_: object, **__: object) -> tuple[None, object]:
        raise EvidenceIncomplete(status, "graph budget exhausted")

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(), dispatcher=fail, install_writer=False, nlp_warmer=lambda: None
    )
    response, after = runtime.dispatch(request(event="Stop"))

    assert after is None
    assert response.status == "ok"
    assert response.exit == 0
    assert response.stdout == ""
    assert response.stderr == ""


@pytest.mark.parametrize("missing", [False, True])
def test_user_prompt_transcript_missing_fails_open_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    from cc_transcript.query import Session

    from captain_hook import Event, on

    state = app.State()
    seen = []
    with app.use_state(state):

        @on(Event.UserPromptSubmit)
        def probe(evt):
            seen.append("entered")
            _ = evt.ctx.t
            seen.append("loaded")

    monkeypatch.setattr("captain_hook.heartbeat.record_heartbeat", lambda *args: None)
    monkeypatch.setattr("captain_hook.cli.after_reply", lambda *args: None)
    transcript = tmp_path / "transcript.jsonl"
    if not missing:
        transcript.write_text("\n")

    def load(path):
        if not Path(path).is_file():
            raise EvidenceIncomplete("missing", "No such file or directory (os error 2)")
        return Session(())

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(state),
        transcript_loader=load,
        install_writer=False,
        nlp_warmer=lambda: None,
    )
    response, after = runtime.dispatch(
        EventRequest(
            id=1,
            event="UserPromptSubmit",
            root=str(tmp_path),
            cwd=str(tmp_path),
            env={"CLAUDE_PROJECT_DIR": str(tmp_path)},
            payload_raw=json.dumps({"transcript_path": str(transcript), "prompt": "synthetic"}),
            client_pid=os.getpid(),
            client_ppid=os.getppid(),
            deadline_unix_ms=int(time.time() * 1000) + 10_000,
        )
    )

    assert response.status == "ok"
    assert response.exit == 0
    assert "Traceback" not in response.stderr
    assert seen == (["entered"] if missing else ["entered", "loaded"])
    if after is not None:
        after()


@pytest.mark.parametrize("status", ["invalid_request", "parse_error", "permission_denied", "stale_handle"])
def test_invalid_evidence_remains_visible(status: str) -> None:
    def fail(*_: object, **__: object) -> tuple[None, object]:
        raise EvidenceIncomplete(status, "invalid transcript evidence")

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(), dispatcher=fail, install_writer=False, nlp_warmer=lambda: None
    )
    response, after = runtime.dispatch(request(event="Stop"))

    assert after is None
    assert response.status == "error"
    assert response.exit == 1
    assert "invalid transcript evidence" in response.stderr


def test_background_snapshot_capacity_failure_does_not_fail_worker() -> None:
    def fail() -> None:
        raise EvidenceIncomplete("retained_limit", "snapshot capacity occupied")

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(),
        dispatcher=lambda *_, **__: (None, fail),
        install_writer=False,
        nlp_warmer=lambda: None,
    )
    response, after = runtime.dispatch(request())

    assert response.exit == 0
    assert after is not None
    after()


@pytest.mark.parametrize("status", ["stale_handle", "stale_cursor"])
def test_expired_graph_evidence_fails_open_after_reply(status: str) -> None:
    def fail() -> None:
        raise GraphEvidenceExpired(status, "prepared graph expired")

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(),
        dispatcher=lambda *_, **__: (None, fail),
        install_writer=False,
        nlp_warmer=lambda: None,
    )
    response, after = runtime.dispatch(request(event="Stop"))

    assert response.exit == 0
    assert after is not None
    after()


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
        env={**os.environ, "CAPTAIN_HOOK_LOG_DIR": str(logs), "CAPT_HOOK_WORKER_SHARD": "0"},
        timeout=180,
    )

    assert worker.returncode == 0, worker.stderr.decode()
    assert (logs / f"daemon-{worker_log_key(importlib.metadata.version('capt-hook'), '0')}.log").exists()


def test_workers_in_different_roots_or_shards_write_different_daemon_logs(tmp_path: Path) -> None:
    import os
    import subprocess

    logs = tmp_path / "logs"
    for root, shard in ((tmp_path / "a", "0"), (tmp_path / "b", "0"), (tmp_path / "a", "1")):
        root.mkdir(exist_ok=True)
        worker = subprocess.run(
            [sys.executable, "-m", "captain_hook.worker"],
            input=frame(hello(importlib.metadata.version("capt-hook"))),
            capture_output=True,
            cwd=root,
            env={**os.environ, "CAPTAIN_HOOK_LOG_DIR": str(logs), "CAPT_HOOK_WORKER_SHARD": shard},
            timeout=180,
        )
        assert worker.returncode == 0, worker.stderr.decode()

    assert len(list(logs.glob("daemon-*.log"))) == 3


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
        env={**os.environ, "CAPTAIN_HOOK_LOG_DIR": str(logs), "CAPT_HOOK_WORKER_SHARD": "0"},
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
