from __future__ import annotations

import importlib.metadata
import io
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from captain_hook import app
from captain_hook.daemon.context import ContextIO, bound_buffers
from captain_hook.dispatch import completion_key
from captain_hook.snapshots.client import AttachmentLimit, EvidenceIncomplete, GraphEvidenceExpired
from captain_hook.types import Event, HookSpec, RegisteredHook
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


def request(
    *, request_id: int = 1, event: str = "PreToolUse", payload_raw: str = "{}", mandatory: bool = False
) -> EventRequest:
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
        mandatory=mandatory,
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


def mandatory_hook(name: str, events: Event = Event.PreToolUse, pack: str | None = "general") -> RegisteredHook:
    return RegisteredHook(
        spec=HookSpec(events=events, mandatory=True), name=name, source_file=f"/hooks/{name}.py", pack_name=pack
    )


def runtime_with(state: app.State, complete: list[str]) -> ProductRuntime:
    def dispatch(root: object, event: object, raw: object, **kwargs: object) -> tuple[None, object]:
        for key in complete:
            reqenv.note_mandatory_completed(key)
        return None, lambda: None

    return ProductRuntime(
        registry_factory=lambda _: FakeRegistry(state),
        dispatcher=dispatch,
        install_writer=False,
        nlp_warmer=lambda: None,
    )


def test_guard_completes_once_every_mandatory_hook_for_the_event_ran() -> None:
    state = app.State()
    state.hooks.extend((mandatory_hook("guard_sessions"), mandatory_hook("second_guard")))
    completed = [completion_key(hook, ordinal) for ordinal, hook in enumerate(state.hooks)]
    response, _ = runtime_with(state, completed).dispatch(request(mandatory=True))
    assert response.guard == "completed"
    assert response.message()["guard"] == "completed"
    assert response.stdout == "discovered out\n"


def test_guard_is_reported_only_to_a_mandatory_request() -> None:
    state = app.State()
    state.hooks.append(mandatory_hook("guard_sessions"))
    response, _ = runtime_with(state, [completion_key(state.hooks[0], 0)]).dispatch(request())
    assert response.guard == ""
    assert "guard" not in response.message()


@pytest.mark.parametrize(
    ("hooks", "complete"),
    [
        ([], []),
        (["guard_sessions"], []),
        (["guard_sessions", "second_guard"], ["guard_sessions"]),
    ],
    ids=["no mandatory hook registered", "the guard never ran", "one of two guards ran"],
)
def test_guard_stays_empty_unless_every_mandatory_hook_completed(hooks: list[str], complete: list[str]) -> None:
    state = app.State()
    state.hooks.extend(mandatory_hook(name) for name in hooks)
    completed = [completion_key(hook, ordinal) for ordinal, hook in enumerate(state.hooks) if hook.name in complete]
    response, _ = runtime_with(state, completed).dispatch(request(mandatory=True))
    assert response.guard == ""


def test_guard_stays_empty_without_a_registered_general_guard() -> None:
    state = app.State()
    state.hooks.append(mandatory_hook("other_guard", pack="other"))
    response, _ = runtime_with(state, [state.hooks[0].state_key]).dispatch(request(mandatory=True))
    assert response.guard == ""


def test_guard_stays_empty_while_the_general_pack_has_a_load_error() -> None:
    state = app.State()
    state.hooks.append(mandatory_hook("guard_sessions"))
    state.load_errors.append(app.LoadError("/hooks/stops.py", ImportError("broken copy"), pack="general"))
    response, _ = runtime_with(state, [state.hooks[0].state_key]).dispatch(request(mandatory=True))
    assert response.guard == ""


def test_guard_counts_another_packs_mandatory_hook_beside_the_general_guard() -> None:
    state = app.State()
    state.hooks.extend((mandatory_hook("guard_sessions"), mandatory_hook("other_guard", pack="other")))
    response, _ = runtime_with(state, [state.hooks[0].state_key]).dispatch(request(mandatory=True))
    assert response.guard == ""
    response, _ = runtime_with(state, [hook.state_key for hook in state.hooks]).dispatch(request(mandatory=True))
    assert response.guard == "completed"


def test_guard_stays_empty_while_a_pack_with_mandatory_hooks_has_a_load_error() -> None:
    state = app.State()
    state.hooks.extend((mandatory_hook("guard_sessions"), mandatory_hook("other_guard", pack="other")))
    state.load_errors.append(app.LoadError("/hooks/other_sibling.py", ImportError("broken copy"), pack="other"))
    response, _ = runtime_with(state, [hook.state_key for hook in state.hooks]).dispatch(request(mandatory=True))
    assert response.guard == ""


def test_guard_ignores_a_load_error_in_a_pack_without_mandatory_hooks() -> None:
    state = app.State()
    state.hooks.append(mandatory_hook("guard_sessions"))
    state.load_errors.append(app.LoadError("/hooks/advisory.py", ImportError("broken copy"), pack="advisory"))
    response, _ = runtime_with(state, [state.hooks[0].state_key]).dispatch(request(mandatory=True))
    assert response.guard == "completed"


def test_guard_counts_only_the_mandatory_hooks_registered_for_the_event() -> None:
    state = app.State()
    state.hooks.extend((mandatory_hook("permission_guard", Event.PermissionRequest), mandatory_hook("guard_sessions")))
    response, _ = runtime_with(state, [completion_key(state.hooks[1], 0)]).dispatch(request(mandatory=True))
    assert response.guard == "completed"
    response, _ = runtime_with(state, [completion_key(state.hooks[1], 0)]).dispatch(
        request(event="PermissionRequest", mandatory=True)
    )
    assert response.guard == ""


def test_a_completion_recorded_twice_does_not_stand_in_for_a_sibling() -> None:
    state = app.State()
    state.hooks.extend((mandatory_hook("guard_sessions"), mandatory_hook("second_guard")))
    response, _ = runtime_with(state, [completion_key(state.hooks[0], 0)] * 2).dispatch(request(mandatory=True))
    assert response.guard == ""
    assert '"permissionDecision": "deny"' in response.stdout
    assert "second_guard did not complete (left unrun)" in response.stdout


def test_the_reserved_lane_follows_the_loaded_registry() -> None:
    state = app.State()
    state.hooks.append(mandatory_hook("guard_sessions"))
    runtime = runtime_with(state, [completion_key(state.hooks[0], 0)])
    assert not runtime.guarded(request())
    assert runtime.guarded(request(mandatory=True))
    runtime.dispatch(request())
    assert runtime.guarded(request())
    assert not runtime.guarded(request(event="PermissionRequest"))
    assert not runtime.guarded(replace(request(), root="/elsewhere"))


def test_guard_stays_empty_when_dispatch_fails() -> None:
    state = app.State()
    state.hooks.append(mandatory_hook("guard_sessions"))

    def fail(*_: object, **__: object) -> tuple[None, object]:
        reqenv.note_mandatory_completed(completion_key(state.hooks[0], 0))
        raise ValueError("broken hook")

    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(state), dispatcher=fail, install_writer=False, nlp_warmer=lambda: None
    )
    response, _ = runtime.dispatch(request(mandatory=True))
    assert response.exit == 1
    assert response.guard == ""


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
    assert "warmups=[]" in served
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


def test_dispatch_reports_a_one_time_load_as_warm_up() -> None:
    class ColdRegistry(FakeRegistry):
        def get(self) -> Snapshot:
            if self.calls == 0:
                reqenv.warmed("registry")
            return super().get()

    runtime = ProductRuntime(
        registry_factory=lambda _: ColdRegistry(),
        dispatcher=lambda *_, **__: (None, lambda: None),
        install_writer=False,
        nlp_warmer=lambda: None,
    )
    first, _ = runtime.dispatch(request(request_id=1))
    second, _ = runtime.dispatch(request(request_id=2))

    assert first.warmup is True
    assert second.warmup is False


def test_nlp_resource_loads_mark_the_bound_request_warm_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook import state

    monkeypatch.setattr(state, "load_spacy", object)
    resources = state.NlpResources()
    requests = [reqenv.RequestOverrides(env={}, cwd="/project", client_ppid=99, session_id="s") for _ in range(2)]
    for overrides in requests:
        with reqenv.use_request(overrides):
            resources.spacy

    assert [overrides.warmups for overrides in requests] == [["spacy"], []]


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


def spawn_refused(*_: object, **__: object) -> tuple[None, object]:
    raise EvidenceIncomplete("cancelled", "posix_spawn python: resource temporarily unavailable")


def session_request(state_dir: Path, *, event: str = "PreToolUse") -> EventRequest:
    return EventRequest(
        id=1,
        event=event,
        root="/project",
        cwd="/project",
        env={"CLAUDE_PROJECT_DIR": "/project", "CAPTAIN_HOOK_STATE_DIR": str(state_dir)},
        payload_raw=json.dumps({"session_id": "fail-open-session"}),
        client_pid=100,
        client_ppid=99,
        deadline_unix_ms=1_700_000_000_000,
    )


def fail_open_runtime() -> ProductRuntime:
    return ProductRuntime(
        registry_factory=lambda _: FakeRegistry(),
        dispatcher=spawn_refused,
        install_writer=False,
        nlp_warmer=lambda: None,
    )


def test_a_failed_dispatch_is_named_once_per_session_across_workers(tmp_path: Path) -> None:
    shards = [fail_open_runtime(), fail_open_runtime()]

    stdouts = []
    for count in range(4):
        response, after = shards[count % 2].dispatch(session_request(tmp_path))
        assert (response.status, response.exit, response.stderr, after) == ("ok", 0, "", None)
        stdouts.append(response.stdout)

    assert stdouts[1:] == ["", "", ""]
    envelope = json.loads(stdouts[0])
    assert envelope["systemMessage"].startswith(
        "capt-hook: skipped all hooks (cancelled: posix_spawn python: resource temporarily unavailable)"
    )
    assert envelope["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse",
        "additionalContext": envelope["systemMessage"],
    }


def test_fail_open_warning_on_stop_never_blocks_the_stop(tmp_path: Path) -> None:
    runtime = fail_open_runtime()

    response, _ = runtime.dispatch(session_request(tmp_path, event="Stop"))

    envelope = json.loads(response.stdout)
    assert set(envelope) == {"systemMessage"}


def test_fail_open_warning_waits_for_an_event_whose_output_is_read(tmp_path: Path) -> None:
    runtime = fail_open_runtime()

    silent = [
        runtime.dispatch(session_request(tmp_path, event=event))[0].stdout
        for event in ("PreCompact", "Notification", "SessionEnd")
    ]
    prompt, _ = runtime.dispatch(session_request(tmp_path, event="UserPromptSubmit"))

    assert silent == ["", "", ""]
    assert "skipped all hooks" in json.loads(prompt.stdout)["systemMessage"]


def skipped_one_hook(*_: object, **__: object) -> tuple[dict[str, object], None]:
    reqenv.evidence_gaps().append("guard: entry_limit: condition incomplete")
    return {"systemMessage": "sibling ran"}, None


def test_a_skipped_hook_is_named_once_and_joins_the_sibling_output(tmp_path: Path) -> None:
    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(),
        dispatcher=skipped_one_hook,
        install_writer=False,
        nlp_warmer=lambda: None,
    )

    first, after = runtime.dispatch(session_request(tmp_path, event="Stop"))
    second, _ = runtime.dispatch(session_request(tmp_path, event="Stop"))

    assert after is not None
    message = json.loads(first.stdout.splitlines()[-1])["systemMessage"]
    assert message.startswith("sibling ran\n\ncapt-hook: skipped guard (entry_limit: condition incomplete)")
    assert json.loads(second.stdout.splitlines()[-1])["systemMessage"] == "sibling ran"


def skipped_one_async_hook(*_: object, **__: object) -> tuple[None, object]:
    return None, lambda: reqenv.evidence_gaps().append("async guard: read_limit: incomplete")


def test_a_hook_skipped_after_the_reply_is_named_on_the_next_dispatch(tmp_path: Path) -> None:
    runtime = ProductRuntime(
        registry_factory=lambda _: FakeRegistry(),
        dispatcher=skipped_one_async_hook,
        install_writer=False,
        nlp_warmer=lambda: None,
    )

    response, after = runtime.dispatch(session_request(tmp_path))
    assert after is not None
    after()
    warned, _ = fail_open_runtime().dispatch(session_request(tmp_path))

    assert "{" not in response.stdout
    assert "skipped async guard (read_limit: incomplete); all hooks" in json.loads(warned.stdout)["systemMessage"]


def test_a_contended_tally_still_fails_open(tmp_path: Path) -> None:
    from filelock import FileLock

    session_dir = tmp_path / "hooks" / "sessions" / "fail-open-session"
    session_dir.mkdir(parents=True)
    runtime = fail_open_runtime()

    with FileLock(str(session_dir / "fail_open_tally.json.lock")):
        response, after = runtime.dispatch(session_request(tmp_path))

    assert (response.status, response.exit, response.stdout, response.stderr, after) == ("ok", 0, "", "", None)


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


def test_worker_skips_the_bundled_cli_version_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.worker.__main__ import skip_bundled_cli_version_probe

    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    skip_bundled_cli_version_probe()
    assert os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] == "1"


@pytest.mark.skipif(os.geteuid() == 0, reason="root forks past RLIMIT_NPROC")
def test_worker_reads_the_process_table_past_its_spawn_nproc_cap() -> None:
    script = """
import resource
from captain_hook.util import proc
from captain_hook.worker.__main__ import lift_spawn_nproc_cap

_, hard = resource.getrlimit(resource.RLIMIT_NPROC)
resource.setrlimit(resource.RLIMIT_NPROC, (1, hard))
assert proc.process_table() is None
lift_spawn_nproc_cap()
soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
assert soft == hard
assert proc.process_table() is not None
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=30)
