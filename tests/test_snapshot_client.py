import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from captain_hook.snapshots.client import (
    CORE_SCHEMA,
    HOST_SCHEMA,
    MAX_VIEW_ATTACHMENTS,
    AttachmentLimit,
    Lease,
    RemoteSession,
    SnapshotClient,
)
from captain_hook.snapshots.worker import empty_usage, failure


def response(request, data, *, cursor=None):
    return {
        "schema": HOST_SCHEMA,
        "response": {
            "schema": CORE_SCHEMA,
            "id": request["id"],
            "status": "incomplete" if cursor else "ok",
            "complete": cursor is None,
            "data": data,
            "cursor": cursor,
            "reason": "page bound" if cursor else None,
            "usage": empty_usage(),
        },
    }


def description(lease="lease", classifier=None):
    return {
        "handle": {"owner_epoch": "owner", "snapshot_id": "snapshot", "generation": "generation", "lease_id": lease},
        "lease_expires_unix_ms": 9_000_000_000_000_000,
        "canonical_path": "/tmp/fixture.jsonl",
        "source_id": "source",
        "device": "1",
        "inode": "2",
        "mtime_ns": "3",
        "ctime_ns": "4",
        "provider": "claude",
        "parser_version": "1",
        "source_bytes": 100,
        "committed_bytes": 100,
        "event_count": 1,
        "turn_count": 1,
        "classifier": classifier or {"id": "native", "version": "1"},
        "provisional_tail": False,
    }


@pytest.mark.parametrize("count", [256, 257, 918, MAX_VIEW_ATTACHMENTS])
def test_deep_query_preserves_all_bounded_attachments(count):
    from captain_hook.snapshots.validation import validate

    requests = []

    def exchange(wrapper):
        validate("host-request", wrapper)
        request = wrapper["request"]
        requests.append(request)
        return response(request, {"kind": "scalar", "value": False})

    client = SnapshotClient(exchange)
    client.bind_tool_registry({})
    source = description()
    attachments = tuple(Path(f"/tmp/attachment-{index}.jsonl") for index in range(count))
    session = RemoteSession(
        client, Lease(client, source), Path(source["canonical_path"]), source["classifier"], attachments=attachments
    )

    assert session.has_edit_to("src/**") is False
    assert requests[0]["view"]["attachments"] == [str(path) for path in attachments]
    assert len(requests) == 1


def test_local_queries_do_not_send_registered_attachments():
    requests = []

    def exchange(wrapper):
        request = wrapper["request"]
        requests.append(request)
        if request["operation"] == "activity_probe":
            return response(
                request,
                {
                    "kind": "activity_probe",
                    "waiting": False,
                    "reason": "no waiting tool",
                    "tool_registry_generation": "fixture",
                },
            )
        value = 1 if request["query"]["kind"] == "event_count" else True
        return response(request, {"kind": "scalar", "value": value})

    client = SnapshotClient(exchange)
    client.bind_tool_registry({})
    source = description()
    attachments = tuple(Path(f"/tmp/attachment-{index}.jsonl") for index in range(918))
    session = RemoteSession(
        client, Lease(client, source), Path(source["canonical_path"]), source["classifier"], attachments=attachments
    )

    assert len(session) == 1
    assert session.has_edit_to("src/**", subagents=False) is True
    assert session.activity_probe(waiting_tools=[], tool_registry_generation="fixture") is False
    assert all(request["view"]["attachments"] == [] for request in requests)


def test_deep_query_over_attachment_bound_is_incomplete_before_transport():
    client = SnapshotClient(lambda _: pytest.fail("overbound view reached transport"))
    client.bind_tool_registry({})
    source = description()
    attachments = tuple(Path(f"/tmp/attachment-{index}.jsonl") for index in range(MAX_VIEW_ATTACHMENTS + 1))
    session = RemoteSession(
        client, Lease(client, source), Path(source["canonical_path"]), source["classifier"], attachments=attachments
    )

    assert len(session.view()["attachments"]) == 0
    with pytest.raises(AttachmentLimit, match="registered transcript attachments exceed 1024"):
        session.has_edit_to("src/**")


def test_real_owner_walks_918_distinct_attachments_with_one_graph_request(tmp_path):
    from dataclasses import replace

    from captain_hook.testing.snapshots import FixtureOwner
    from tests.helpers import raw_text
    from tests.test_snapshot_fixture_owner import write_messages

    source = tmp_path / "root.jsonl"
    write_messages(source, raw_text("user", "root"))
    attachments = tuple(tmp_path / f"attachment-{index}.jsonl" for index in range(918))
    for path in attachments:
        write_messages(path, raw_text("user", "attached"))
    fixture = FixtureOwner()
    try:
        session = replace(fixture.load(source), attachments=attachments)
        requests = []
        exchange = fixture.client._exchange

        def record(request):
            requests.append(request["request"])
            return exchange(request)

        fixture.client._exchange = record
        assert session.has_edit_to("src/**") is False
        graph_requests = [request for request in requests if request["operation"] == "query"]
        assert len(graph_requests) == 1
        assert graph_requests[0]["view"]["attachments"] == [str(path) for path in attachments]
        session.release()
    finally:
        fixture.close()


def test_abandoned_page_releases_cursor_through_cleanup_exchange():
    foreground = []
    cleanup = []

    def exchange(wrapper):
        req = wrapper["request"]
        foreground.append(req)
        return response(req, {"kind": "strings", "values": ["first"]}, cursor="cursor")

    def release(wrapper):
        req = wrapper["request"]
        cleanup.append(req)
        return response(req, {"kind": "released", "released": True})

    client = SnapshotClient(exchange, cleanup_exchange=release)
    pages = client.pages("query", view={}, query={})
    assert next(pages) == {"kind": "strings", "values": ["first"]}
    pages.close()
    assert len(foreground) == 1
    assert cleanup[0]["operation"] == "release"
    assert cleanup[0]["kind"] == "cursor"
    assert "owner_epoch" not in cleanup[0]


def test_exitstack_lease_cleanup_retries_retained_limit():
    requests = []

    def cleanup(wrapper):
        request = wrapper["request"]
        requests.append(request)
        if len(requests) == 1:
            return {"schema": HOST_SCHEMA, "response": failure(request["id"], "retained_limit", "busy")}
        return response(request, {"kind": "released", "released": True})

    client = SnapshotClient(lambda _: pytest.fail("foreground exchange used"), cleanup_exchange=cleanup)
    client.bind_tool_registry({})
    lease = Lease(client, description())
    with ExitStack() as stack:
        stack.callback(lease.release)

    assert lease.released
    assert len(requests) == 2
    assert requests[0]["id"] != requests[1]["id"]
    assert all(request["operation"] == "release" for request in requests)


def test_exhausted_release_capacity_preserves_original_error(monkeypatch):
    from captain_hook.snapshots import client as snapshot_client

    monkeypatch.setattr(snapshot_client, "CLEANUP_SECONDS", 0)
    requests = []

    def cleanup(wrapper):
        request = wrapper["request"]
        requests.append(request)
        if len(requests) == 1:
            return {"schema": HOST_SCHEMA, "response": failure(request["id"], "retained_limit", "busy")}
        return response(request, {"kind": "released", "released": True})

    client = SnapshotClient(lambda _: pytest.fail("foreground exchange used"), cleanup_exchange=cleanup)
    client.bind_tool_registry({})
    lease = Lease(client, description())
    with pytest.raises(ValueError, match="original failure"):
        with ExitStack() as stack:
            stack.callback(lease.release)
            raise ValueError("original failure")
    assert not lease.released
    client.close()
    assert len(requests) == 1
    lease.release()
    assert lease.released
    assert len(requests) == 2


def test_release_does_not_retry_other_failures():
    from captain_hook.snapshots.client import EvidenceIncomplete

    requests = []

    def cleanup(wrapper):
        request = wrapper["request"]
        requests.append(request)
        return {"schema": HOST_SCHEMA, "response": failure(request["id"], "invalid_request", "bad token")}

    client = SnapshotClient(lambda _: pytest.fail("foreground exchange used"), cleanup_exchange=cleanup)
    client.bind_tool_registry({})
    lease = Lease(client, description())
    with pytest.raises(EvidenceIncomplete, match="bad token"):
        lease.release()
    assert len(requests) == 1
    assert not lease.released


def test_configured_classifier_runs_in_callers_context_for_each_preparation(monkeypatch):
    from captain_hook.util import reqenv
    from tests.test_classifiers import user_event

    event = user_event("message")
    module = SimpleNamespace(
        decode_projection=lambda schema, records, *, tool_registry: [event],
        SnapshotIncomplete=type("SnapshotIncomplete", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "cc_transcript.snapshots", module)
    labels = []
    operations = []

    def exchange(wrapper):
        req = wrapper["request"]
        operations.append(req["operation"])
        if req["operation"] == "prepare_classifier":
            return response(
                req,
                {
                    "kind": "classification",
                    "record_schema": "cc-transcript.event/1",
                    "records_json": ["owned fixture"],
                    "event_start": 4,
                },
                cursor="classifier-cursor",
            )
        if req["operation"] == "submit_classifier":
            labels.append(req["labels"])
            return response(
                req,
                {
                    "kind": "acquired",
                    "description": description(
                        f"derived-{len(labels)}", {"id": "derived", "version": str(len(labels))}
                    ),
                },
            )
        assert req["operation"] == "release"
        return response(req, {"kind": "released", "released": True})

    client = SnapshotClient(exchange)
    for value in ("yes", "no"):
        session = RemoteSession(
            client, Lease(client, description()), Path("/tmp/fixture.jsonl"), {"id": "native", "version": "1"}
        )
        scope = reqenv.RequestOverrides({"HOOKS_CLASSIFY": value}, "/tmp", 0, "fixture")
        with reqenv.use_request(scope):
            classified = client.classify(
                session,
                lambda _: reqenv.getenv("HOOKS_CLASSIFY") == "yes",
                {"id": "configured", "version": "unchanged-code"},
            )
            assert classified.classifier == {"id": "derived", "version": str(len(labels))}
            assert session.lease.released
            classified.release()
    assert labels == [[True], [False]]
    assert not client._leases


def test_classifier_callback_failure_releases_its_cursor(monkeypatch):
    from tests.test_classifiers import user_event

    module = SimpleNamespace(
        decode_projection=lambda schema, records, *, tool_registry: [user_event("message")],
        SnapshotIncomplete=type("SnapshotIncomplete", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "cc_transcript.snapshots", module)
    cleanup = []

    def exchange(wrapper):
        req = wrapper["request"]
        if req["operation"] == "release":
            cleanup.append(req)
            return response(req, {"kind": "released", "released": True})
        return response(
            req,
            {
                "kind": "classification",
                "record_schema": "cc-transcript.event/1",
                "records_json": ["owned fixture"],
                "event_start": 0,
            },
            cursor="classifier-cursor",
        )

    def callback(event):
        raise ValueError("configured callback failed")

    client = SnapshotClient(exchange)
    session = RemoteSession(
        client, Lease(client, description()), Path("/tmp/fixture.jsonl"), {"id": "native", "version": "1"}
    )
    with pytest.raises(ValueError, match="configured callback failed"):
        client.classify(session, callback, {"id": "configured", "version": "fixture"})
    assert cleanup[0]["kind"] == "cursor"
    session.release()


def test_tool_registry_is_captured_once_and_cannot_change_during_preparation():
    from captain_hook.snapshots.client import EvidenceIncomplete

    captured = []

    def exchange(wrapper):
        captured.append(wrapper["tool_registry"])
        return response(wrapper["request"], {"kind": "released", "released": False})

    fields = {"path": "file", "content": "replacement"}
    client = SnapshotClient(exchange)
    client.bind_tool_registry({"mcp_edit": ("Edit", fields)})
    fields["content"] = "changed"
    client.call("stats")
    client.call("stats")
    assert (
        captured
        == [
            [
                {
                    "name": "mcp_edit",
                    "behaves_like": "Edit",
                    "span_edit": {"path": "file", "content": "replacement", "delete": None},
                }
            ]
        ]
        * 2
    )
    with pytest.raises(EvidenceIncomplete, match="changed during one evidence preparation"):
        client.bind_tool_registry({"mcp_edit": ("Edit", fields)})


def test_lease_renews_only_near_its_reported_expiry(monkeypatch):
    monkeypatch.setattr("captain_hook.snapshots.client.time.time", lambda: 100.0)
    calls = []

    def exchange(wrapper):
        req = wrapper["request"]
        calls.append(req)
        return response(req, {"kind": "renewed", "expires_unix_ms": 110_000})

    client = SnapshotClient(exchange)
    details = description() | {"lease_expires_unix_ms": 100_500}
    lease = Lease(client, details)
    assert lease.require() == details["handle"]
    assert lease.require() == details["handle"]
    assert len(calls) == 1
    assert calls[0]["operation"] == "renew"
    assert lease.expires_unix_ms == 110_000


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        (None, None),
        ("source_id", "other"),
        ("mtime_ns", "4"),
        ("ctime_ns", "5"),
        ("source_bytes", 101),
        ("committed_bytes", 99),
        ("provisional_tail", True),
    ],
)
def test_builtin_classifier_acquires_a_matching_lease_and_checks_source_revision(changed_field, changed_value):
    from captain_hook.snapshots.client import EvidenceIncomplete

    classifier = {"id": "captain-lane", "version": "1"}
    initial = description("initial")
    derived = description("classified", classifier)
    derived["handle"] = dict(derived["handle"], snapshot_id="derived", generation="derived")
    if changed_field is not None:
        derived[changed_field] = changed_value
    requests = []

    def exchange(wrapper):
        request = wrapper["request"]
        requests.append(request)
        if request["operation"] == "acquire":
            assert request["classifier"] == classifier
            return response(request, {"kind": "acquired", "description": derived})
        assert request["operation"] == "release"
        return response(request, {"kind": "released", "released": True})

    client = SnapshotClient(exchange)
    original = RemoteSession(client, Lease(client, initial), Path(initial["canonical_path"]), initial["classifier"])
    try:
        if changed_field is not None:
            with pytest.raises(EvidenceIncomplete, match="transcript changed during classifier selection"):
                original.with_classifier(classifier)
            assert not original.lease.released
            assert requests[-1]["token"] == "classified"
        else:
            classified = original.with_classifier(classifier)
            assert original.lease.released
            assert classified.view()["classifier"] == classifier
            assert classified.view()["handle"] == derived["handle"]
            assert requests[-1]["token"] == "initial"
    finally:
        client.close()
    assert not client._leases


def test_builtin_classifier_keeps_its_existing_matching_lease():
    requests = []

    def exchange(wrapper):
        request = wrapper["request"]
        requests.append(request["operation"])
        assert request["operation"] == "release"
        return response(request, {"kind": "released", "released": True})

    client = SnapshotClient(exchange)
    source = description()
    session = RemoteSession(client, Lease(client, source), Path(source["canonical_path"]), source["classifier"])
    assert session.with_classifier(source["classifier"]) is session
    assert not requests
    session.release()
    assert requests == ["release"]


def test_selected_subagents_keep_child_classifiers_and_reuse_owned_leases(tmp_path):
    from dataclasses import replace

    from captain_hook.testing.snapshots import FixtureOwner
    from tests.helpers import raw_assistant, raw_text, raw_tool_use
    from tests.test_snapshot_fixture_owner import write_messages

    directory = tmp_path / "conductor-workspaces"
    directory.mkdir()
    source = directory / "session.jsonl"
    write_messages(
        source,
        raw_text("user", "first"),
        raw_assistant(raw_tool_use("Agent", {"subagent_type": "first", "prompt": "go"}, "first")),
        raw_text("user", "second"),
        raw_assistant(raw_tool_use("Agent", {"subagent_type": "second", "prompt": "go"}, "second")),
    )
    children = directory / "session" / "subagents"
    children.mkdir(parents=True)
    write_messages(children / "agent-first.jsonl", raw_text("user", "first child"))
    write_messages(children / "agent-second.jsonl", raw_text("user", "second child"))
    nested = children / "agent-second" / "subagents"
    nested.mkdir(parents=True)
    (nested / "agent-invalid.jsonl").write_text("not json\n")
    attachment = directory / "unrelated.jsonl"
    attachment.write_text("not json\n")
    fixture = FixtureOwner()
    try:
        session = replace(fixture.load(source), attachments=(attachment,))
        assert session.classifier == {"id": "captain-conductor", "version": "1"}
        index = session.current_turn.subagents
        assert [item.id for item in index] == ["second"]
        child = index.with_type("second")[0].session
        assert child.classifier == {"id": "native", "version": "1"}
        assert child.user_text == "second child"
        before = fixture.client.call("stats")["data"]["counters"]
        assert before["cold_parses"] == 2
        for _ in range(3):
            assert session.current_turn.subagents is index
        after = fixture.client.call("stats")["data"]["counters"]
        assert after["source_opens"] == before["source_opens"]
        assert len(fixture.client._leases) == 2
        session.release()
        assert child.lease.released
        assert not fixture.client._leases
    finally:
        fixture.close()
