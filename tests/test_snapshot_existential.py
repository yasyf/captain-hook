import json
from contextlib import contextmanager

import pytest

from captain_hook.conditions import EditedSource
from captain_hook.context import HookContext
from captain_hook.events import StopEvent
from captain_hook.session import SessionStore
from captain_hook.snapshots.client import HOST_SCHEMA, EvidenceIncomplete, GraphEvidenceExpired, GraphSources, SnapshotClient, SnapshotProtocolError
from captain_hook.snapshots.worker import failure
from captain_hook.testing.snapshots import FixtureOwner
from tests.helpers import raw_assistant, raw_text, raw_tool_use
from tests.test_prepared_graph_evidence import session_with_sources
from tests.test_snapshot_client import response
from tests.test_snapshot_fixture_owner import write_messages


def inputs_page(label, *, cursor=None):
    return {
        "data": {
            "kind": "records",
            "record_schema": "cc-transcript.predicate-inputs/1",
            "records_json": [json.dumps({"calls": [], "commands": [label], "edited_files": [], "skills": []})],
        },
        "cursor": cursor,
    }


@contextmanager
def paged_session(attempts, *, defer_cleanup=False):
    operations = []
    active = iter(())
    prepare_count = 0
    attempt = 0

    def exchange(wrapper):
        nonlocal active, prepare_count, attempt
        request = wrapper["request"]
        operation = request["operation"]
        operations.append(request)
        if operation == "prepare_graph":
            prepare_count += 1
            return response(request, {"kind": "prepared_graph", "handle": {
                "graph_id": f"graph-{prepare_count}", "owner_epoch": "owner", "revision": "rev", "complete": True,
            }})
        if operation == "release":
            return response(request, None)
        if operation == "query_graph":
            active = iter(attempts[attempt])
            attempt += 1
        else:
            assert operation == "resume"
        page = next(active)
        if isinstance(page, str):
            return {"schema": HOST_SCHEMA, "response": failure(request["id"], page, "fixture changed")}
        result = response(request, page["data"], cursor=page["cursor"])
        result["response"].update(page.get("envelope", {}))
        return result

    client = SnapshotClient(exchange)
    client.bind_tool_registry({})
    client._defer_cleanup = defer_cleanup
    session = session_with_sources(client, 918)
    try:
        yield session, operations
    finally:
        session.release()
        client.close()


def cursor_releases(operations):
    return [request["token"] for request in operations if request["operation"] == "release" and request["kind"] == "cursor"]


def test_existential_positive_closes_first_page_without_reading_later_pages():
    with paged_session([[inputs_page("positive", cursor="first"), inputs_page("unused")]]) as (session, operations):
        assert session.any_deep_input(lambda window: window.commands[0] == "positive") is True
        assert not any(request["operation"] == "resume" for request in operations)
        assert cursor_releases(operations) == ["first"]


def test_existential_negative_exhausts_the_validated_final_page():
    with paged_session([[inputs_page("first", cursor="first"), inputs_page("last")]]) as (session, operations):
        visited = []
        assert session.any_deep_input(lambda window: visited.append(window.commands[0]) or False) is False
        assert visited == ["first", "last"]
        assert sum(request["operation"] == "resume" for request in operations) == 1


def test_existential_restarts_the_whole_boolean_attempt_after_a_late_stale_handle():
    attempts = [[inputs_page("old", cursor="old"), "stale_handle"], [inputs_page("new", cursor="new"), inputs_page("positive")]]
    with paged_session(attempts) as (session, operations):
        visited = []

        def predicate(window):
            visited.append(window.commands[0])
            return window.commands[0] == "positive"

        assert session.any_deep_input(predicate) is True
        assert visited == ["old", "new", "positive"]
        assert sum(request["operation"] == "prepare_graph" for request in operations) == 2
        assert sum(request["operation"] == "query_graph" for request in operations) == 2


@pytest.mark.parametrize("status", ["changed", "stale_cursor"])
def test_existential_late_failure_cannot_become_a_complete_negative(status):
    with paged_session([[inputs_page("negative", cursor="first"), status]]) as (session, operations):
        with pytest.raises(EvidenceIncomplete) as caught:
            session.any_deep_input(lambda _: False)
        assert caught.value.status == status
        assert sum(request["operation"] == "prepare_graph" for request in operations) == 1


def test_existential_stale_retry_is_bounded():
    with paged_session([["stale_handle"], ["stale_handle"]]) as (session, operations):
        with pytest.raises(GraphEvidenceExpired):
            session.any_deep_input(lambda _: False)
        assert sum(request["operation"] == "query_graph" for request in operations) == 2


def test_existential_predicate_exception_closes_the_owned_cursor():
    def predicate(_):
        raise ValueError("predicate failed")

    with paged_session([[inputs_page("first", cursor="first"), inputs_page("unused")]]) as (session, operations):
        with pytest.raises(ValueError, match="predicate failed"):
            session.any_deep_input(predicate)
        assert cursor_releases(operations) == ["first"]
        assert not any(request["operation"] == "resume" for request in operations)


def test_existential_cursor_close_preserves_deferred_cleanup():
    with paged_session([[inputs_page("positive", cursor="first")]], defer_cleanup=True) as (session, operations):
        assert session.any_deep_input(lambda _: True) is True
        assert cursor_releases(operations) == []
        assert session.client._deferred_cursors == {("first", None)}
        session.client.close_pending()
        assert cursor_releases(operations) == ["first"]
        assert session.client._deferred_cursors == set()


@pytest.mark.parametrize(("envelope", "cursor", "error"), [
    ({"status": "ok", "complete": True}, "unexpected", SnapshotProtocolError),
    ({"status": "ok", "complete": False}, None, SnapshotProtocolError),
    ({"status": "incomplete", "complete": False}, None, EvidenceIncomplete),
])
def test_existential_validates_the_page_envelope_before_accepting_positive(envelope, cursor, error):
    page = inputs_page("positive", cursor=cursor) | {"envelope": envelope}
    with paged_session([[page]]) as (session, operations):
        visited = []
        with pytest.raises(error):
            session.any_deep_input(lambda window: visited.append(window) or True)
        assert visited == []
        assert cursor_releases(operations) == ([cursor] if cursor else [])


def test_delegated_source_edit_avoids_later_native_fact_pages(tmp_path, monkeypatch, record_property):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = tmp_path / "root.jsonl"
    write_messages(root, raw_text("user", "delegate"))
    ids = tuple(f"thread-{index:02d}" for index in range(16))
    member_bytes = 0
    for index, thread in enumerate(ids):
        path = tmp_path / f"{thread}.jsonl"
        tool = raw_tool_use("Edit", {"file_path": "src/app.py", "old_string": "a", "new_string": "b"}) if index == 0 else raw_tool_use("Read", {"file_path": "unused.md"})
        write_messages(path, raw_text("user", "delegate"), raw_assistant(tool))
        assert path.stat().st_size <= 1024
        member_bytes += path.stat().st_size
    fixture = FixtureOwner()
    fixture.client.bind_tool_registry({})
    operations = []
    exchange = fixture.client._exchange

    def record(wrapper):
        operations.append(wrapper["request"])
        return exchange(wrapper)

    fixture.client._exchange = record
    fixture.client._cleanup_exchange = record
    try:
        transcript = fixture.load(root).with_registered_sources(GraphSources(thread_ids=ids, roots=(tmp_path,)))
        try:
            before = fixture.client.call("stats")["data"]["counters"]
            operations.clear()
            event = StopEvent(_raw={}, ctx=HookContext(session=SessionStore(None), transcript=transcript, settings=None, project_root=tmp_path))
            assert EditedSource().check(event) is True
            after = fixture.client.call("stats")["data"]["counters"]
            metrics = {name: after[name] - before[name] for name in ("source_opens", "cold_parses", "source_bytes_read", "prepared_cache_bytes_read")}
            metrics["query_pages"] = sum(request["operation"] in {"query_graph", "resume"} for request in operations)
            metrics["member_bytes"] = member_bytes
            record_property("native_metrics", json.dumps(metrics, sort_keys=True))
            assert 0 < metrics["source_opens"] < len(ids), metrics
            assert 0 < metrics["cold_parses"] < len(ids), metrics
            assert 0 < metrics["source_bytes_read"] < member_bytes, metrics
            assert metrics["query_pages"] == 1, metrics
            assert len(cursor_releases(operations)) == 1
            assert not any(request["operation"].startswith("warm_") for request in operations)
        finally:
            transcript.release()
    finally:
        fixture.close()
