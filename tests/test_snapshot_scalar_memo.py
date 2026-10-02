from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from captain_hook.snapshots.client import (
    HOST_SCHEMA,
    EvidenceIncomplete,
    GraphSources,
    Lease,
    RemoteSession,
    SnapshotClient,
)
from captain_hook.snapshots.worker import failure
from tests.test_snapshot_client import description, response


class CountingOwner:
    def __init__(self, *, status="ok"):
        self.queries = []
        self.status = status

    def __call__(self, wrapper):
        request = wrapper["request"]
        if request["operation"] == "prepare_graph":
            return response(
                request,
                {
                    "kind": "prepared_graph",
                    "handle": {"graph_id": "graph", "owner_epoch": "owner", "revision": "1", "complete": True},
                },
            )
        if request["operation"] == "release":
            return response(request, {"kind": "released", "released": True})
        self.queries.append(request)
        if self.status != "ok":
            return {"schema": HOST_SCHEMA, "response": failure(request["id"], self.status, "fixture")}
        return response(request, {"kind": "scalar", "value": len(self.queries)})


def session_for(owner, *, lease="lease"):
    client = SnapshotClient(owner)
    client.bind_tool_registry({})
    source = description(lease)
    return RemoteSession(client, Lease(client, source), Path(source["canonical_path"]), source["classifier"])


def test_identical_scalar_query_reuses_the_complete_result():
    owner = CountingOwner()
    session = session_for(owner)

    assert len(session) == 1
    assert len(session) == 1
    assert session.has_edit_to("src/**", subagents=False) == 2
    assert session.has_edit_to("src/**", subagents=False) == 2
    assert len(owner.queries) == 2


def test_different_query_or_selector_performs_one_exchange():
    owner = CountingOwner()
    session = session_for(owner)

    assert len(session) == 1
    assert session.has_edit_to("src/**", subagents=False) == 2
    assert session.has_edit_to("docs/**", subagents=False) == 3
    assert len(session.current_turn) == 4
    assert len(session.current_turn) == 4
    assert len(owner.queries) == 4


def test_sibling_session_on_the_same_lease_shares_the_memo():
    owner = CountingOwner()
    session = session_for(owner)

    assert session.first_prompt == 1
    assert session.with_registered_sources(GraphSources()).first_prompt == 1
    assert len(owner.queries) == 1


def test_deep_scalar_is_keyed_by_prepared_graph():
    owner = CountingOwner()
    session = session_for(owner)
    registered = session.with_registered_sources(GraphSources(thread_ids=("thread",)))

    assert session.has_tool("Bash") == 1
    assert session.has_tool("Bash") == 1
    assert registered.has_tool("Bash") == 2
    assert len(owner.queries) == 2


def test_released_lease_drops_its_memo_and_refuses_reuse():
    owner = CountingOwner()
    session = session_for(owner)

    assert len(session) == 1
    session.release()

    assert session.lease.scalar_results == {}
    with pytest.raises(EvidenceIncomplete, match="already released"):
        len(session)


def test_new_lease_performs_one_exchange():
    owner = CountingOwner()
    first = session_for(owner, lease="first")
    second = RemoteSession(first.client, Lease(first.client, description("second")), first.path, first.classifier)

    assert len(first) == 1
    assert len(second) == 2
    assert len(owner.queries) == 2


@pytest.mark.parametrize("status", ["deadline", "stale_handle", "incomplete", "cancelled"])
def test_incomplete_results_are_never_memoized(status):
    owner = CountingOwner(status=status)
    session = session_for(owner)

    for _ in range(2):
        with pytest.raises(EvidenceIncomplete):
            len(session)
    assert len(owner.queries) == 2
    assert session.lease.scalar_results == {}


def test_release_racing_a_hit_is_incomplete_evidence(monkeypatch):
    owner = CountingOwner()
    session = session_for(owner)
    assert len(session) == 1
    require = session.lease.require

    def require_then_release(client=None):
        handle = require(client)
        monkeypatch.setattr(session.lease, "require", require)
        session.release()
        return handle

    monkeypatch.setattr(session.lease, "require", require_then_release)

    with pytest.raises(EvidenceIncomplete, match="already released"):
        len(session)
    assert len(owner.queries) == 1


def test_released_lease_does_not_cache_an_in_flight_scalar_result():
    owner = CountingOwner()
    entered = Event()
    resume = Event()

    def exchange(wrapper):
        result = owner(wrapper)
        if wrapper["request"]["operation"] == "query":
            entered.set()
            assert resume.wait(2)
        return result

    session = session_for(exchange)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(session.query, {"kind": "event_count"})
        try:
            assert entered.wait(2)
            session.release()
            assert session.lease.closed
            assert session.lease.scalar_results == {}
        finally:
            resume.set()
        assert future.result(timeout=2) == 1

    assert session.lease.scalar_results == {}
    assert len(owner.queries) == 1
