import time
from pathlib import Path

import pytest

from captain_hook.snapshots.client import (
    EvidenceIncomplete,
    GraphSources,
    Lease,
    RemoteSession,
    SnapshotClient,
)
from tests.test_snapshot_client import description


class RecordingClient(SnapshotClient):
    def __init__(self, *, fail_prepare=False):
        super().__init__(lambda _: {})
        self.requests = []
        self.cleanups = []
        self.prepare_count = 0
        self.fail_prepare = fail_prepare
        self.fail_query = None
        self.bind_tool_registry({})

    def call(self, operation, **arguments):
        assert operation == "release"
        self.cleanups.append(arguments)
        return {"status": "ok"}

    def pages(self, operation, **arguments):
        self.requests.append((operation, arguments))
        if operation == "retain":
            yield {"kind": "acquired", "description": description(lease="retained")}
        elif operation == "prepare_graph":
            self.prepare_count += 1
            if self.fail_prepare:
                raise EvidenceIncomplete("incomplete", "source budget exhausted")
            yield {
                "kind": "prepared_graph",
                "handle": {"graph_id": "graph", "owner_epoch": "owner", "revision": "rev", "complete": True},
            }
        elif operation == "query_graph":
            if self.fail_query is not None:
                raise EvidenceIncomplete(self.fail_query, "prepared graph expired")
            yield {"kind": "scalar", "value": False}
        elif operation == "query":
            yield {"kind": "scalar", "value": True}
        else:
            raise AssertionError(operation)


def session_with_sources(client, count):
    source = description()
    session = RemoteSession(client, Lease(client, source), Path(source["canonical_path"]), source["classifier"])
    return session.with_registered_sources(GraphSources(thread_ids=tuple(f"thread-{i}" for i in range(count))))


@pytest.mark.parametrize("count", [1, 256, 918, 1024])
def test_multiple_deep_checks_prepare_once_and_query_by_handle(count):
    client = RecordingClient()
    session = session_with_sources(client, count)

    assert session.has_edit_to("src/**") is False
    assert session.has_read("README.md") is False
    assert client.prepare_count == 1
    operation, prepare = client.requests[0]
    assert operation == "prepare_graph"
    assert prepare["thread_ids"] == [f"thread-{i}" for i in range(count)]
    assert prepare["view"]["attachments"] == []
    assert prepare["limits"]["max_read_bytes"] == 1024 * 1024
    assert prepare["limits"]["max_discovery_entries"] == 50_000
    assert prepare["limits"]["max_sources"] == 4096
    assert 0 < prepare["deadline_unix_ms"] - int(time.time() * 1000) <= 750
    for operation, arguments in client.requests[1:]:
        assert operation == "query_graph"
        assert arguments["handle"] == {"graph_id": "graph", "owner_epoch": "owner", "revision": "rev", "complete": True}
        assert arguments["deadline_unix_ms"] == prepare["deadline_unix_ms"]
        assert arguments["limits"] == prepare["limits"]
        assert "view" not in arguments
        assert "thread_ids" not in arguments
        assert "direct_paths" not in arguments


def test_incomplete_preparation_is_shared_without_a_second_read():
    client = RecordingClient(fail_prepare=True)
    session = session_with_sources(client, 918)

    for query in (lambda: session.has_edit_to("src/**"), lambda: session.has_read("README.md")):
        with pytest.raises(EvidenceIncomplete, match="source budget exhausted"):
            query()
    assert client.prepare_count == 1
    assert [operation for operation, _ in client.requests] == ["prepare_graph"]


def test_incomplete_graph_enqueues_registered_sources_for_warming():
    client = RecordingClient(fail_prepare=True)
    queued = []
    client._warm_scheduler = lambda source_client, sources: queued.append((source_client, sources))
    session = session_with_sources(client, 918)

    with pytest.raises(EvidenceIncomplete, match="source budget exhausted"):
        session.has_edit_to("src/**")

    assert queued == [(client, session.graph.sources)]


def test_cold_root_schedules_background_progress_and_fails_open(tmp_path):
    from captain_hook.snapshots.client import CURRENT_CLIENT
    from captain_hook.transcripts import load_transcript

    path = tmp_path / "large-root.jsonl"
    scheduled = []
    client = SnapshotClient(
        lambda _: pytest.fail("cold root should not complete in foreground"),
        root_warm_scheduler=lambda source_client, source_path, classifier: scheduled.append(
            (source_client, source_path, classifier)
        ),
    )
    client.acquire = lambda _: (_ for _ in ()).throw(EvidenceIncomplete("incomplete", "foreground byte budget"))
    token = CURRENT_CLIENT.set(client)
    try:
        with pytest.raises(EvidenceIncomplete, match="foreground byte budget"):
            load_transcript(path)
    finally:
        CURRENT_CLIENT.reset(token)

    assert scheduled == [(client, path, {"id": "native", "version": "1"})]


def test_graph_work_stays_inside_the_hook_deadline():
    from captain_hook.util import reqenv

    client = RecordingClient()
    session = session_with_sources(client, 1)
    deadline_unix_ms = int(time.time() * 1000) + 1400
    scope = reqenv.RequestOverrides({}, "/tmp", 0, "session", deadline_unix_ms=deadline_unix_ms)

    with reqenv.use_request(scope):
        assert session.has_edit_to("src/**") is False

    assert client.requests[0][1]["deadline_unix_ms"] == deadline_unix_ms - 1000
    assert client.requests[1][1]["deadline_unix_ms"] == deadline_unix_ms - 1000


def test_unrelated_event_does_not_prepare_registered_transcripts(tmp_path, monkeypatch):
    from captain_hook import EditedSource, on
    from captain_hook.cli import dispatch_event
    from captain_hook.events import Event

    @on(Event.Stop, only_if=[EditedSource()])
    def stop_gate(_):
        pytest.fail("unrelated Stop hook ran")

    monkeypatch.setattr(
        "captain_hook.transcripts.registered_sources", lambda _: pytest.fail("registered sources were read")
    )
    monkeypatch.setattr("captain_hook.cli.after_reply", lambda *args: None)
    _, background = dispatch_event(
        tmp_path,
        Event.UserPromptSubmit,
        {"session_id": "unrelated", "transcript_path": str(tmp_path / "main.jsonl")},
        session_dir=None,
        transcript_loader=lambda _: pytest.fail("transcript was loaded"),
    )
    background()


def test_local_query_view_cannot_contain_registry_paths():
    client = RecordingClient()
    session = session_with_sources(client, 918)

    assert session.has_read("README.md", subagents=False) is True
    assert session.view()["attachments"] == []
    assert client.requests == [
        (
            "query",
            {
                "view": session.view(),
                "query": {"kind": "has_read", "pattern": "README.md", "subagents": False},
            },
        )
    ]
    assert client.prepare_count == 0


def test_sync_and_background_share_one_preparation_across_clients(tmp_path, monkeypatch):
    from captain_hook.cli import dispatch_event
    from captain_hook.events import Event
    from captain_hook.snapshots.client import CURRENT_CLIENT
    from captain_hook.transcripts import release_transcript

    calls = []
    source = description()

    class PhaseClient(RecordingClient):
        def pages(self, operation, **arguments):
            if operation == "retain":
                calls.append((self.phase, operation, arguments))
                yield {"kind": "acquired", "description": description(lease="background")}
                return
            calls.append((self.phase, operation, arguments))
            yield from super().pages(operation, **arguments)

        def call(self, operation, **arguments):
            calls.append((self.phase, operation, arguments))
            return {"status": "ok"}

    foreground = PhaseClient()
    foreground.phase = "foreground"
    background_client = PhaseClient()
    background_client.phase = "background"
    monkeypatch.setattr(
        "captain_hook.transcripts.registered_sources",
        lambda _: GraphSources(thread_ids=("thread-1",)),
    )

    def loader(_):
        return RemoteSession(
            foreground,
            Lease(foreground, source),
            Path(source["canonical_path"]),
            source["classifier"],
        )

    def sync(_event, evt, session_dir):
        assert evt.ctx.t.has_edit_to("src/**") is False
        release_transcript(evt.ctx.transcript)
        return None

    def after(_event, evt, raw, session_dir):
        assert evt.ctx.t.has_read("README.md") is False

    monkeypatch.setattr("captain_hook.cli.dispatch", sync)
    monkeypatch.setattr("captain_hook.cli.after_reply", after)
    token = CURRENT_CLIENT.set(foreground)
    try:
        _, background = dispatch_event(
            tmp_path,
            Event.Stop,
            {"session_id": "s-shared", "transcript_path": str(tmp_path / "root.jsonl")},
            session_dir=None,
            transcript_loader=loader,
        )
        phase_token = CURRENT_CLIENT.set(background_client)
        try:
            background()
        finally:
            CURRENT_CLIENT.reset(phase_token)
    finally:
        CURRENT_CLIENT.reset(token)

    assert sum(operation == "prepare_graph" for _, operation, _ in calls) == 1
    assert [(phase, operation) for phase, operation, _ in calls if operation == "query_graph"] == [
        ("foreground", "query_graph"),
        ("background", "query_graph"),
    ]
    assert sum(operation == "release" and args.get("kind") == "graph" for _, operation, args in calls) == 1


def test_classifier_change_preserves_prepared_graph_sources(monkeypatch):
    client = RecordingClient()
    session = session_with_sources(client, 918)
    classifier = {"id": "configured", "version": "1"}

    def acquire(path, *, classifier):
        changed = description(lease="classified", classifier=classifier)
        return RemoteSession(client, Lease(client, changed), path, classifier)

    monkeypatch.setattr(client, "acquire", acquire)
    classified = session.with_classifier(classifier)

    assert classified.graph.sources.thread_ids == session.graph.sources.thread_ids
    assert classified.has_edit_to("src/**") is False
    assert client.prepare_count == 1


def test_graph_cleanup_uses_parentless_transport_after_foreground_closes():
    from captain_hook.snapshots.client import PreparedGraphEvidence, PreparedGraphHandle
    from tests.test_snapshot_client import response

    cleanup = []

    def foreground(_):
        raise AssertionError("foreground transport was used after reply")

    def parentless(wrapper):
        cleanup.append(wrapper["request"])
        return response(wrapper["request"], {"kind": "released", "released": True})

    client = SnapshotClient(foreground, cleanup_exchange=parentless)
    client.bind_tool_registry({})
    graph = PreparedGraphEvidence(GraphSources())
    graph.outcome = PreparedGraphHandle("prepared", "owner", "rev")
    graph.client = client

    graph.release()

    assert len(cleanup) == 1
    assert cleanup[0]["operation"] == "release"
    assert cleanup[0]["kind"] == "graph"
    assert cleanup[0]["token"] == "prepared"


def test_remote_session_type_has_no_path_bearing_query_view():
    import inspect
    from dataclasses import fields

    assert "attachments" not in {field.name for field in fields(RemoteSession)}
    assert "deep" not in inspect.signature(RemoteSession.view).parameters


def test_query_graph_schema_requires_an_opaque_handle():
    from jsonschema import ValidationError

    from captain_hook.snapshots.client import CORE_SCHEMA, DEFAULT_LIMITS, HOST_SCHEMA
    from captain_hook.snapshots.validation import validate

    request = {
        "schema": CORE_SCHEMA,
        "id": "graph-query",
        "operation": "query_graph",
        "deadline_unix_ms": 1_790_389_614_377,
        "limits": DEFAULT_LIMITS,
        "selectors": [],
        "query": {"kind": "has_edit_to", "values": ["*"], "subagents": True},
    }
    envelope = {"schema": HOST_SCHEMA, "request": request, "tool_registry": []}

    with pytest.raises(ValidationError):
        validate("host-request", envelope)
    validate(
        "host-request",
        envelope
        | {
            "request": request
            | {"handle": {"graph_id": "g", "owner_epoch": "owner", "revision": "rev", "complete": True}}
        },
    )


def test_graph_call_preserves_its_explicit_work_budget():
    from captain_hook.snapshots.client import DEFAULT_LIMITS
    from tests.test_snapshot_client import response

    requests = []

    def exchange(wrapper):
        request = wrapper["request"]
        requests.append(request)
        return response(request, {"kind": "scalar", "value": False})

    client = SnapshotClient(exchange)
    client.bind_tool_registry({})
    limits = DEFAULT_LIMITS | {"max_read_bytes": 32 * 1024 * 1024}
    deadline_unix_ms = 1_790_389_614_377

    client.call(
        "query_graph",
        handle={"graph_id": "g", "owner_epoch": "owner", "revision": "rev", "complete": True},
        selectors=[],
        query={"kind": "has_edit_to", "values": ["*.py"], "subagents": True},
        limits=limits,
        deadline_unix_ms=deadline_unix_ms,
    )

    assert requests[0]["limits"] == limits
    assert requests[0]["deadline_unix_ms"] == deadline_unix_ms


def test_cancelled_branch_does_not_close_another_branches_graph():
    client = RecordingClient()
    session = session_with_sources(client, 918)
    survivor = session.retain()

    session.release()
    assert not any(cleanup["kind"] == "graph" for cleanup in client.cleanups)
    assert survivor.has_edit_to("src/**") is False
    assert client.prepare_count == 1
    survivor.release()
    assert sum(cleanup["kind"] == "graph" for cleanup in client.cleanups) == 1


def test_classifier_change_after_graph_preparation_is_incomplete_without_reread():
    client = RecordingClient()
    session = session_with_sources(client, 918)
    assert session.has_edit_to("src/**") is False

    with pytest.raises(EvidenceIncomplete, match="classifier changed after graph preparation"):
        session.with_classifier({"id": "configured", "version": "1"})
    assert client.prepare_count == 1


def test_ordinary_query_schema_rejects_attachment_paths():
    from jsonschema import ValidationError

    from captain_hook.snapshots.client import CORE_SCHEMA, DEFAULT_LIMITS, HOST_SCHEMA
    from captain_hook.snapshots.validation import validate

    view = {
        "handle": description()["handle"],
        "classifier": {"id": "native", "version": "1"},
        "selectors": [],
        "attachments": [],
    }
    request = {
        "schema": CORE_SCHEMA,
        "id": "local-query",
        "operation": "query",
        "deadline_unix_ms": 1_790_389_614_377,
        "limits": DEFAULT_LIMITS,
        "view": view,
        "query": {"kind": "event_count"},
    }
    envelope = {"schema": HOST_SCHEMA, "request": request, "tool_registry": []}

    validate("host-request", envelope)
    with pytest.raises(ValidationError):
        validate(
            "host-request", envelope | {"request": request | {"view": view | {"attachments": ["/tmp/read-again"]}}}
        )


def test_expired_prepared_graph_query_is_typed_for_fail_open():
    from captain_hook.snapshots.client import GraphEvidenceExpired

    client = RecordingClient()
    client.fail_query = "stale_handle"
    session = session_with_sources(client, 918)

    with pytest.raises(GraphEvidenceExpired, match="prepared graph expired"):
        session.has_edit_to("src/**")
    assert client.prepare_count == 1


def test_failed_graph_release_retries_when_request_client_closes(monkeypatch):
    from captain_hook.snapshots import client as snapshot_client
    from captain_hook.snapshots.client import PreparedGraphEvidence, PreparedGraphHandle
    from captain_hook.snapshots.worker import failure
    from tests.test_snapshot_client import response

    releases = []

    def cleanup(wrapper):
        request = wrapper["request"]
        releases.append(request)
        if len(releases) == 1:
            return {"schema": snapshot_client.HOST_SCHEMA, "response": failure(request["id"], "retained_limit", "busy")}
        return response(request, {"kind": "released", "released": True})

    client = SnapshotClient(lambda _: pytest.fail("foreground transport used"), cleanup_exchange=cleanup)
    client.bind_tool_registry({})
    graph = PreparedGraphEvidence(GraphSources())
    graph.outcome = PreparedGraphHandle("graph", "owner", "rev")
    graph.client = client
    client._graphs.add(graph)
    monkeypatch.setattr(snapshot_client, "CLEANUP_SECONDS", 0)

    graph.release()
    assert not graph.native_released
    client.close()
    assert graph.native_released
    assert len(releases) == 2
    assert not client._graphs
