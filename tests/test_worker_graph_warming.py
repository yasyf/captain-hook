import io
import json
from pathlib import Path

import pytest

from captain_hook.snapshots.client import (
    WARM_INTERVAL_SECONDS,
    WARM_STEP_BYTES,
    WARM_STEP_SECONDS,
    EvidenceIncomplete,
    GraphSources,
    RegisteredWarmState,
    RootWarmState,
    SnapshotClient,
    warm_interval,
)
from captain_hook.worker.protocol import EventResponse, read_message
from captain_hook.worker.service import MAX_WARM_DEMAND_BYTES, MAX_WARM_DEMANDS, WARM_DEMAND_SECONDS, WorkerService

LIMITS = {
    "max_read_bytes": 512 * 1024 * 1024,
    "max_source_read_bytes": 512 * 1024 * 1024,
    "max_events": 1_000_000,
    "max_items": 65_536,
    "max_output_bytes": 16 * 1024 * 1024,
    "max_discovery_entries": 50_000,
    "max_sources": 4096,
}
HOST_STEP_FIELDS = {"id", "deadline_unix_ms", "start_index", "membership_revision"}


def service():
    return WorkerService(io.BytesIO(), io.BytesIO(), dispatch=lambda _: (EventResponse(), None))


def close(service):
    service._close_snapshots()
    service._executor.shutdown()
    service._background.shutdown()
    service._cleanup.shutdown()


def client():
    result = SnapshotClient(lambda _: {})
    result.bind_tool_registry({})
    return result


def at(monkeypatch, seconds):
    monkeypatch.setattr("captain_hook.worker.service.time.monotonic", lambda: seconds)


def frames(service):
    stream = io.BytesIO(service._output.getvalue())
    return list(iter(lambda: read_message(stream), None))


def warm_frame(demand):
    return {"protocol": 1, "op": "snapshot_warm", "snapshot": demand}


def registered_demand(thread_ids, *, roots=(), direct_paths=(), active_since_unix_ms=None, tool_registry=()):
    return {
        "schema": "captain.transcript/1",
        "request": {
            "schema": "cc-transcript.snapshot/1",
            "operation": "warm_registered",
            "classifier": {"id": "native", "version": "1"},
            "thread_ids": list(thread_ids),
            "roots": list(roots),
            "direct_paths": list(direct_paths),
            "active_since_unix_ms": active_since_unix_ms,
            "limits": LIMITS,
        },
        "tool_registry": list(tool_registry),
    }


def root_demand(path, classifier, tail_bytes):
    return {
        "schema": "captain.transcript/1",
        "request": {
            "schema": "cc-transcript.snapshot/1",
            "operation": "warm_root",
            "path": path,
            "classifier": classifier,
            "tail_bytes": tail_bytes,
            "limits": LIMITS,
        },
        "tool_registry": [],
    }


def test_repeated_events_post_one_demand_for_a_scope(monkeypatch):
    worker = service()
    sources = GraphSources(
        thread_ids=("one", "two"),
        roots=(Path("/sessions"),),
        direct_paths=(Path("/logs/direct.jsonl"),),
        active_since_unix_ms=1_700_000_000_000,
    )
    at(monkeypatch, 1000)
    try:
        for _ in range(20):
            worker.schedule_graph_warm(client(), sources)
        assert frames(worker) == [
            warm_frame(
                registered_demand(
                    ["one", "two"],
                    roots=["/sessions"],
                    direct_paths=["/logs/direct.jsonl"],
                    active_since_unix_ms=1_700_000_000_000,
                )
            )
        ]
    finally:
        close(worker)


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        pytest.param(GraphSources(thread_ids=("one", "two")), registered_demand(["one", "two"]), id="thread-added"),
        pytest.param(
            GraphSources(thread_ids=("one",), roots=(Path("/sessions"),)),
            registered_demand(["one"], roots=["/sessions"]),
            id="root-added",
        ),
        pytest.param(
            GraphSources(thread_ids=("one",), direct_paths=(Path("/logs/direct.jsonl"),)),
            registered_demand(["one"], direct_paths=["/logs/direct.jsonl"]),
            id="direct-path-added",
        ),
        pytest.param(
            GraphSources(thread_ids=("one",), active_since_unix_ms=1_700_000_000_000),
            registered_demand(["one"], active_since_unix_ms=1_700_000_000_000),
            id="window-moved",
        ),
    ],
)
def test_changed_scope_posts_its_own_demand(monkeypatch, changed, expected):
    worker = service()
    at(monkeypatch, 1000)
    try:
        worker.schedule_graph_warm(client(), GraphSources(thread_ids=("one",)))
        worker.schedule_graph_warm(client(), changed)
        worker.schedule_graph_warm(client(), GraphSources(thread_ids=("one",)))
        assert frames(worker) == [warm_frame(registered_demand(["one"])), warm_frame(expected)]
    finally:
        close(worker)


def test_scope_is_demanded_again_only_after_the_renewal_interval(monkeypatch):
    worker = service()
    sources = GraphSources(thread_ids=("one",))
    demand = warm_frame(registered_demand(["one"]))
    try:
        at(monkeypatch, 1000)
        worker.schedule_graph_warm(client(), sources)
        at(monkeypatch, 1000 + WARM_DEMAND_SECONDS - 1)
        worker.schedule_graph_warm(client(), sources)
        assert frames(worker) == [demand]
        at(monkeypatch, 1000 + WARM_DEMAND_SECONDS)
        worker.schedule_graph_warm(client(), sources)
        assert frames(worker) == [demand, demand]
        at(monkeypatch, 1000 + 2 * WARM_DEMAND_SECONDS - 1)
        worker.schedule_graph_warm(client(), sources)
        assert frames(worker) == [demand, demand]
    finally:
        close(worker)


def test_demand_carries_the_foreground_tool_registry(monkeypatch):
    worker = service()
    source_client = SnapshotClient(lambda _: {})
    source_client.bind_tool_registry({"custom_write": ("Write", {"file": "path"})})
    at(monkeypatch, 1000)
    try:
        worker.schedule_graph_warm(source_client, GraphSources(thread_ids=("one",)))
        worker.schedule_graph_warm(client(), GraphSources(thread_ids=("one",)))
        assert frames(worker) == [
            warm_frame(
                registered_demand(
                    ["one"],
                    tool_registry=[
                        {"name": "custom_write", "behaves_like": "Write", "span_edit": {"file": "path", "delete": None}}
                    ],
                )
            ),
            warm_frame(registered_demand(["one"])),
        ]
    finally:
        close(worker)


def test_root_and_registered_scopes_post_their_own_demands(monkeypatch, tmp_path):
    worker = service()
    source_client = SnapshotClient(
        lambda _: {}, warm_scheduler=worker.schedule_graph_warm, root_warm_scheduler=worker.schedule_root_warm
    )
    source_client.bind_tool_registry({})
    path = tmp_path / "root.jsonl"
    at(monkeypatch, 1000)
    try:
        source_client.schedule_root_warm(path, {"id": "native", "version": "1"}, 4 * 1024 * 1024)
        source_client.schedule_root_warm(str(path), {"id": "captain-configured", "version": "policy"}, None)
        source_client.schedule_root_warm(str(path), {"id": "captain-lane", "version": "1"}, None)
        source_client.schedule_root_warm(path, {"id": "native", "version": "1"}, None)
        source_client.schedule_graph_warm(GraphSources(thread_ids=("one",)))
        assert frames(worker) == [
            warm_frame(root_demand(str(path), {"id": "native", "version": "1"}, 4 * 1024 * 1024)),
            warm_frame(root_demand(str(path), {"id": "captain-lane", "version": "1"}, None)),
            warm_frame(root_demand(str(path), {"id": "native", "version": "1"}, None)),
            warm_frame(registered_demand(["one"])),
        ]
    finally:
        close(worker)


def test_sources_without_members_post_nothing():
    worker = service()
    try:
        worker.schedule_graph_warm(client(), GraphSources(roots=(Path("/sessions"),), active_since_unix_ms=1))
        assert frames(worker) == []
        assert worker._warm_demands == {}
    finally:
        close(worker)


@pytest.mark.parametrize(
    ("excess", "posted"),
    [pytest.param(0, True, id="at-the-bound"), pytest.param(1, False, id="one-byte-over")],
)
def test_demand_is_posted_only_within_its_frame_bound(excess, posted):
    worker = service()
    smallest = json.dumps(registered_demand([], direct_paths=["/"]), sort_keys=True, separators=(",", ":"))
    path = "/" + "p" * (MAX_WARM_DEMAND_BYTES - len(smallest) + excess)
    try:
        worker.schedule_graph_warm(client(), GraphSources(direct_paths=(Path(path),)))
        assert frames(worker) == ([warm_frame(registered_demand([], direct_paths=[path]))] if posted else [])
    finally:
        close(worker)


def test_full_demand_table_stays_bounded_and_evicted_scope_posts_again(monkeypatch):
    worker = service()
    source_client = client()
    at(monkeypatch, 1000)
    try:
        for index in range(MAX_WARM_DEMANDS + 1):
            worker.schedule_graph_warm(source_client, GraphSources(thread_ids=(str(index),)))
        assert len(worker._warm_demands) == MAX_WARM_DEMANDS
        worker.schedule_graph_warm(source_client, GraphSources(thread_ids=("2",)))
        worker.schedule_graph_warm(source_client, GraphSources(thread_ids=("0",)))
        assert len(worker._warm_demands) == MAX_WARM_DEMANDS
        assert [frame["snapshot"]["request"]["thread_ids"] for frame in frames(worker)] == [
            *([str(index)] for index in range(MAX_WARM_DEMANDS + 1)),
            ["0"],
        ]
    finally:
        close(worker)


def test_renewed_scope_outlives_older_scopes_in_a_full_demand_table(monkeypatch):
    worker = service()
    source_client = client()
    try:
        at(monkeypatch, 1000)
        for index in range(MAX_WARM_DEMANDS):
            worker.schedule_graph_warm(source_client, GraphSources(thread_ids=(str(index),)))
        at(monkeypatch, 1000 + WARM_DEMAND_SECONDS)
        worker.schedule_graph_warm(source_client, GraphSources(thread_ids=("0",)))
        worker.schedule_graph_warm(source_client, GraphSources(thread_ids=("new",)))
        worker.schedule_graph_warm(source_client, GraphSources(thread_ids=("0",)))
        assert len(worker._warm_demands) == MAX_WARM_DEMANDS
        assert [frame["snapshot"]["request"]["thread_ids"] for frame in frames(worker)] == [
            *([str(index)] for index in range(MAX_WARM_DEMANDS)),
            ["0"],
            ["new"],
        ]
    finally:
        close(worker)


def test_worker_posts_demand_without_stepping_the_warm_itself(monkeypatch, tmp_path):
    worker = service()
    source_client = client()
    at(monkeypatch, 1000)
    try:
        worker.schedule_root_warm(source_client, tmp_path / "root.jsonl", {"id": "native", "version": "1"}, None)
        worker.schedule_graph_warm(source_client, GraphSources(thread_ids=("one",)))
        posted = frames(worker)
        assert [frame["op"] for frame in posted] == ["snapshot_warm", "snapshot_warm"]
        assert [set(frame) for frame in posted] == [{"protocol", "op", "snapshot"}] * 2
        assert [HOST_STEP_FIELDS & frame["snapshot"]["request"].keys() for frame in posted] == [set(), set()]
        assert worker._snapshot_next_id == 0
        assert worker._snapshot_pending == {}
    finally:
        close(worker)


def test_warm_step_has_small_independent_budget_and_progress(monkeypatch):
    calls = []

    class Client:
        def call(self, operation, **arguments):
            calls.append((operation, arguments))
            return {
                "status": "ok",
                "usage": {"source_bytes_read": 1, "discovery_entries_examined": 0},
                "data": {
                    "kind": "warmed_registry",
                    "owner_epoch": "owner",
                    "membership_revision": "revision",
                    "next_index": 3,
                    "complete": False,
                    "source_offset": 0,
                    "source_size": 0,
                    "fact_cache_bytes": 100,
                    "fact_cache_write_bytes": 200,
                    "fact_cache_writes": 2,
                },
            }

    state = RegisteredWarmState(GraphSources(thread_ids=("one", "two", "three")), Client())
    with monkeypatch.context() as clock:
        clock.setattr("captain_hook.snapshots.client.time.time", lambda: 100.0)
        assert state.step(read_bytes=WARM_STEP_BYTES, deadline_seconds=WARM_STEP_SECONDS) is False
    operation, arguments = calls[0]
    assert operation == "warm_registered"
    assert arguments["limits"]["max_source_read_bytes"] == 8 * 1024 * 1024
    assert arguments["deadline_unix_ms"] == 103_000
    assert state.start_index == 3
    assert state.fact_cache_write_bytes == 200
    assert state.step(read_bytes=WARM_STEP_BYTES, deadline_seconds=WARM_STEP_SECONDS) is False
    assert calls[1][1]["start_index"] == 3
    assert calls[1][1]["membership_revision"] == "revision"


def test_warm_step_sends_the_scope_fields_a_demand_names(monkeypatch):
    calls = []

    class Client:
        def call(self, operation, **arguments):
            calls.append(arguments)
            return {
                "status": "changed",
                "reason": "membership changed",
                "usage": {"source_bytes_read": 0, "discovery_entries_examined": 0},
            }

    sources = GraphSources(
        thread_ids=("one", "two"),
        roots=(Path("/sessions"),),
        direct_paths=(Path("/logs/direct.jsonl"),),
        active_since_unix_ms=1_700_000_000_000,
    )

    state = RegisteredWarmState(sources, Client())
    with monkeypatch.context() as clock:
        clock.setattr("captain_hook.snapshots.client.time.time", lambda: 100.0)
        assert state.step(read_bytes=WARM_STEP_BYTES, deadline_seconds=WARM_STEP_SECONDS) is False
    assert calls == [
        {
            "classifier": {"id": "native", "version": "1"},
            "thread_ids": ["one", "two"],
            "roots": ["/sessions"],
            "direct_paths": ["/logs/direct.jsonl"],
            "active_since_unix_ms": 1_700_000_000_000,
            "start_index": 0,
            "membership_revision": None,
            "deadline_unix_ms": 103_000,
            "limits": LIMITS | {"max_source_read_bytes": 8 * 1024 * 1024},
        }
    ]


@pytest.mark.parametrize(
    ("usage", "paced"),
    [
        pytest.param({"source_bytes_read": 0, "prepared_cache_bytes_read": 4096}, True, id="prepared-cache-read"),
        pytest.param({"source_bytes_read": 4096, "prepared_cache_bytes_read": 0}, True, id="source-read"),
        pytest.param(
            {"source_bytes_read": 0, "prepared_cache_bytes_read": 0, "cache_hits": 8}, False, id="memory-only"
        ),
    ],
)
def test_warming_paces_every_step_that_read_source_or_prepared_cache_bytes(usage, paced):
    assert warm_interval(usage) == (WARM_INTERVAL_SECONDS if paced else 0)


def test_root_warmer_advances_bounded_source_without_restarting():
    calls = []
    offsets = iter((8 * 1024 * 1024, 16 * 1024 * 1024, 16 * 1024 * 1024))

    class Client:
        def call(self, operation, **arguments):
            calls.append((operation, arguments))
            offset = next(offsets)
            return {
                "status": "ok",
                "usage": {"source_bytes_read": 8 * 1024 * 1024 if offset < 16 * 1024 * 1024 else 0},
                "data": {
                    "kind": "warmed_root",
                    "owner_epoch": "owner",
                    "source_revision": "revision",
                    "source_offset": offset,
                    "source_size": 16 * 1024 * 1024,
                    "complete": len(calls) == 3,
                    "facts_complete": len(calls) == 3,
                },
            }

    state = RootWarmState(Path("/tmp/root.jsonl"), {"id": "native", "version": "1"}, Client())
    assert state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is False
    assert state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is False
    assert state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is True
    assert all(operation == "warm_root" for operation, _ in calls)
    assert all(arguments["limits"]["max_source_read_bytes"] == 8 * 1024 * 1024 for _, arguments in calls)
    assert state.stalled_steps == 0


def test_root_warmer_requires_finished_facts_after_bytes_are_complete():
    class Client:
        def call(self, operation, **arguments):
            return {
                "status": "ok",
                "usage": {"source_bytes_read": 0, "events_parsed": 0},
                "data": {
                    "kind": "warmed_root",
                    "owner_epoch": "owner",
                    "source_revision": "revision",
                    "source_offset": 202 * 1024 * 1024,
                    "source_size": 202 * 1024 * 1024,
                    "complete": True,
                    "facts_complete": False,
                },
            }

    state = RootWarmState(Path("/tmp/root.jsonl"), {"id": "native", "version": "1"}, Client())
    assert state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is False
    assert state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is False
    with pytest.raises(EvidenceIncomplete, match="made no progress"):
        state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3)


def test_changed_membership_resets_warming_progress():
    calls = []

    class Client:
        def call(self, operation, **arguments):
            calls.append(arguments)
            return {
                "status": "changed",
                "reason": "membership changed",
                "usage": {"source_bytes_read": 0, "discovery_entries_examined": 0},
            }

    state = RegisteredWarmState(GraphSources(thread_ids=("one",)), Client())
    state.start_index = 4
    state.membership_revision = "old"

    assert state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is False
    assert calls[0]["membership_revision"] == "old"
    assert state.start_index == 0
    assert state.membership_revision is None


def test_warmer_stops_after_two_steps_without_native_progress():
    class Client:
        def call(self, operation, **arguments):
            return {
                "status": "incomplete",
                "reason": "source unavailable",
                "usage": {"source_bytes_read": 0, "discovery_entries_examined": 0},
            }

    state = RegisteredWarmState(GraphSources(thread_ids=("one",)), Client())

    assert state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is False
    with pytest.raises(EvidenceIncomplete, match="made no progress"):
        state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3)


def test_source_offset_proves_progress_despite_a_fixed_member_index():
    offsets = iter((8 * 1024 * 1024, 16 * 1024 * 1024))

    class Client:
        def call(self, operation, **arguments):
            return {
                "status": "ok",
                "usage": {"source_bytes_read": 128, "discovery_entries_examined": 0},
                "data": {
                    "kind": "warmed_registry",
                    "owner_epoch": "owner",
                    "membership_revision": "revision",
                    "next_index": 0,
                    "complete": False,
                    "source_offset": next(offsets),
                    "source_size": 40 * 1024 * 1024,
                    "fact_cache_bytes": 0,
                    "fact_cache_write_bytes": 0,
                    "fact_cache_writes": 0,
                },
            }

    state = RegisteredWarmState(GraphSources(thread_ids=("large",)), Client())
    assert state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is False
    assert state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is False
    assert state.stalled_steps == 0
    assert state.source_offset == 16 * 1024 * 1024


def test_parsed_events_prove_progress_after_source_bytes_are_loaded():
    calls = 0

    class Client:
        def call(self, operation, **arguments):
            nonlocal calls
            calls += 1
            return {
                "status": "ok",
                "usage": {"source_bytes_read": 0, "events_parsed": 128},
                "data": {
                    "kind": "warmed_registry",
                    "owner_epoch": "owner",
                    "membership_revision": "revision",
                    "next_index": 0,
                    "complete": False,
                    "source_offset": 8 * 1024 * 1024,
                    "source_size": 8 * 1024 * 1024,
                    "fact_cache_bytes": 0,
                    "fact_cache_write_bytes": 0,
                    "fact_cache_writes": 0,
                },
            }

    state = RegisteredWarmState(GraphSources(thread_ids=("large",)), Client())
    assert state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is False
    assert state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is False
    assert state.stalled_steps == 0
    assert calls == 2
