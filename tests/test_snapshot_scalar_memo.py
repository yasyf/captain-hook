import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from captain_hook.app import hook
from captain_hook.context import HookContext
from captain_hook.dispatch import prepare_hook_events
from captain_hook.events import PreToolUseEvent
from captain_hook.session import SessionStore
from captain_hook.snapshots.client import (
    HOST_SCHEMA,
    EvidenceIncomplete,
    GraphSources,
    Lease,
    RemoteSession,
    SnapshotClient,
    tool_predicate,
)
from captain_hook.testing.snapshots import FixtureOwner, failure
from captain_hook.transcripts import lazy_transcript, release_transcript
from captain_hook.types import Event as HookEvent
from captain_hook.types import UsedTool
from tests.helpers import raw_assistant, raw_text, raw_tool_use
from tests.test_snapshot_client import description, response

BASH = tool_predicate("has_tool", "Bash", subagents=False)
IN_SESSION = UsedTool("Bash", subagents=False, scope="session")
IN_TURN = UsedTool("Bash", subagents=False)


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
        if request["operation"] == "retain":
            return response(request, {"kind": "acquired", "description": description(f"retained-{request['id']}")})
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


def test_deep_scalar_memo_hit_requires_an_open_prepared_graph():
    owner = CountingOwner()
    session = session_for(owner)

    assert session.has_tool("Bash") == 1
    assert session.has_tool("Bash") == 1
    session.graph.release()

    with pytest.raises(EvidenceIncomplete, match="prepared graph is closed"):
        session.has_tool("Bash")
    assert len(owner.queries) == 1


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


@pytest.fixture
def ran_bash_a_turn_ago(tmp_path):
    path = tmp_path / "session.jsonl"
    lines = [
        raw_text("user", "list the files"),
        raw_assistant(raw_tool_use("Bash", {"command": "ls"}, "listed")),
        raw_text("user", "now continue"),
        raw_assistant({"type": "text", "text": "ok"}),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def dispatch_queries(tmp_path, transcript, conditions):
    owner = FixtureOwner()
    exchange = owner.client._exchange
    queries = []

    def record(wrapper):
        request = wrapper["request"]
        if request["operation"] == "query":
            queries.append((request["view"]["selectors"], request["query"]))
        return exchange(wrapper)

    owner.client._exchange = record
    try:
        for condition in conditions:
            hook(HookEvent.PreToolUse, "fixture", only_if=[condition])
        source = lazy_transcript(transcript, loader=owner.load)
        evt = PreToolUseEvent(_raw={"tool_name": "Bash"}, ctx=HookContext(SessionStore(tmp_path), source, None))
        matched, forks = prepare_hook_events(evt, async_=False)
        for fork in forks:
            release_transcript(fork.ctx.transcript)
        return len(matched), queries
    finally:
        owner.close()


@pytest.mark.parametrize("forks", [pytest.param(1, id="one-fork"), pytest.param(50, id="fifty-forks")])
def test_one_dispatch_asks_the_owner_once_however_many_forks_share_a_predicate(tmp_path, ran_bash_a_turn_ago, forks):
    matched, queries = dispatch_queries(tmp_path, ran_bash_a_turn_ago, [IN_SESSION] * forks)

    assert matched == forks
    assert queries == [([], BASH)]


def test_forks_on_different_selectors_each_get_their_own_answer(tmp_path, ran_bash_a_turn_ago):
    matched, queries = dispatch_queries(tmp_path, ran_bash_a_turn_ago, [IN_SESSION, IN_TURN] * 25)

    assert matched == 25
    assert queries == [([], BASH), ([{"kind": "current_turn"}], BASH)]


def test_a_retained_branch_reuses_its_source_answer_and_outlives_a_sibling_release():
    owner = CountingOwner()
    session = session_for(owner)
    first, second = session.retain(), session.retain()

    assert len(first) == 1
    first.release()
    assert len(second) == 1
    assert len(session.current_turn) == 2
    assert len(owner.queries) == 2
    assert first.lease.scalar_results == {}
    assert second.lease.scalar_results is session.lease.scalar_results
