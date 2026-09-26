import io
import threading
import time
from pathlib import Path

import pytest

from captain_hook.snapshots.client import (
    EvidenceIncomplete,
    GraphSources,
    RegisteredWarmState,
    RootWarmState,
    SnapshotClient,
)
from captain_hook.worker.protocol import EventResponse
from captain_hook.worker.service import WarmJob, WorkerService


def service():
    return WorkerService(io.BytesIO(), io.BytesIO(), dispatch=lambda _: (EventResponse(), None))


def close(service):
    with service._warm_guard:
        service._warm_stop.set()
    service._close_snapshots()
    service._warm_executor.shutdown(wait=True)
    service._executor.shutdown()
    service._background.shutdown()
    service._cleanup.shutdown()


def client():
    result = SnapshotClient(lambda _: {})
    result.bind_tool_registry({})
    return result


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_repeated_events_share_one_active_warmer(monkeypatch):
    import captain_hook.worker.service as module

    worker = service()
    started = threading.Event()
    finish = threading.Event()
    calls = []

    def step(job):
        calls.append(job.state.sources.thread_ids)
        started.set()
        assert finish.wait(2)
        return True

    monkeypatch.setattr(module, "WARM_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(worker, "_warm_step", step)
    sources = GraphSources(thread_ids=("one",), session_key="session")
    try:
        worker.schedule_graph_warm(client(), sources)
        assert started.wait(2)
        for _ in range(20):
            worker.schedule_graph_warm(client(), sources)
        finish.set()
        wait_until(lambda: not worker._warm_jobs)
        assert calls == [("one",)]
    finally:
        finish.set()
        close(worker)


def test_new_membership_supersedes_active_warmer(monkeypatch):
    import captain_hook.worker.service as module

    worker = service()
    started = threading.Event()
    finish = threading.Event()
    calls = []

    def step(job):
        calls.append(job.state.sources.thread_ids)
        if job.state.sources.thread_ids == ("old",):
            started.set()
            assert finish.wait(2)
        return True

    monkeypatch.setattr(module, "WARM_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(worker, "_warm_step", step)
    try:
        worker.schedule_graph_warm(client(), GraphSources(thread_ids=("old",), session_key="session"))
        assert started.wait(2)
        worker.schedule_graph_warm(client(), GraphSources(thread_ids=("new",), session_key="session"))
        finish.set()
        wait_until(lambda: not worker._warm_jobs)
        assert calls == [("old",), ("new",)]
    finally:
        finish.set()
        close(worker)


def test_full_warm_queue_admits_new_owner_and_evicted_owner_can_return(monkeypatch):
    import captain_hook.worker.service as module

    worker = service()
    started = threading.Event()
    finish = threading.Event()
    served = []

    def step(job):
        served.append(job.owner_key)
        if job.owner_key == "owner-0":
            started.set()
            assert finish.wait(2)
        return True

    monkeypatch.setattr(module, "WARM_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(worker, "_warm_step", step)
    source_client = client()
    try:
        worker.schedule_graph_warm(source_client, GraphSources(thread_ids=("0",), session_key="owner-0"))
        assert started.wait(2)
        for index in range(1, 33):
            worker.schedule_graph_warm(
                source_client,
                GraphSources(thread_ids=(str(index),), session_key=f"owner-{index}"),
            )
        assert len(worker._warm_jobs) == module.MAX_WARM_JOBS
        finish.set()
        wait_until(lambda: not worker._warm_jobs)
        assert "owner-32" in served
        assert "owner-31" not in served
        worker.schedule_graph_warm(source_client, GraphSources(thread_ids=("31",), session_key="owner-31"))
        wait_until(lambda: "owner-31" in served)
    finally:
        finish.set()
        close(worker)


def test_warm_step_has_small_independent_budget_and_progress():
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

    worker = service()
    job = WarmJob(
        "session",
        "fingerprint",
        RegisteredWarmState(GraphSources(thread_ids=("one", "two", "three"), session_key="session"), Client()),
    )
    try:
        started = int(time.time() * 1000)
        assert worker._warm_step(job) is False
        operation, arguments = calls[0]
        assert operation == "warm_registered"
        assert arguments["limits"]["max_read_bytes"] == 8 * 1024 * 1024
        assert started < arguments["deadline_unix_ms"] <= started + 3000
        assert job.state.start_index == 3
        assert job.state.fact_cache_write_bytes == 200
        assert worker._warm_step(job) is False
        assert calls[1][1]["start_index"] == 3
        assert calls[1][1]["membership_revision"] == "revision"
    finally:
        close(worker)


def test_warmer_uses_the_foreground_tool_registry(monkeypatch):
    worker = service()
    source_client = SnapshotClient(lambda _: {})
    source_client.bind_tool_registry({"custom_write": ("Write", {"file": "path"})})
    observed = []

    def step(job):
        observed.append(job.state.client.tool_registry())
        return True

    monkeypatch.setattr(worker, "_warm_step", step)
    try:
        worker.schedule_graph_warm(source_client, GraphSources(thread_ids=("one",), session_key="session"))
        wait_until(lambda: not worker._warm_jobs)
        assert observed == [source_client.tool_registry()]
    finally:
        close(worker)


def test_root_and_registered_warming_share_one_paced_fair_worker(monkeypatch, tmp_path):
    import captain_hook.worker.service as module

    worker = service()
    observed = []
    started = threading.Event()
    advance = threading.Event()

    def step(job):
        observed.append(type(job.state).__name__)
        if len(observed) == 1:
            started.set()
            assert advance.wait(2)
        job.state.last_usage = {"source_bytes_read": 0}
        return observed.count(type(job.state).__name__) == 2

    monkeypatch.setattr(module, "WARM_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(worker, "_warm_step", step)
    source_client = client()
    try:
        worker.schedule_root_warm(source_client, tmp_path / "root.jsonl", {"id": "native", "version": "1"})
        assert started.wait(2)
        worker.schedule_graph_warm(source_client, GraphSources(thread_ids=("one",), session_key="session"))
        advance.set()
        wait_until(lambda: not worker._warm_jobs)
        assert observed == ["RootWarmState", "RegisteredWarmState", "RootWarmState", "RegisteredWarmState"]
    finally:
        advance.set()
        close(worker)


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
    assert all(arguments["limits"]["max_read_bytes"] == 8 * 1024 * 1024 for _, arguments in calls)
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
