import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from captain_hook.snapshots.client import CORE_SCHEMA, HOST_SCHEMA, Lease, RemoteSession, SnapshotClient
from captain_hook.snapshots.worker import empty_usage


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
