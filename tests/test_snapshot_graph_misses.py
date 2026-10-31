import threading
from types import SimpleNamespace

from captain_hook.snapshots import worker
from captain_hook.snapshots.worker import GRAPH_MISS_SECONDS, MAX_GRAPH_MISSES, Owner, empty_usage, failure
from captain_hook.util.caching import LRUDict
from tests.test_snapshot_owner import context, request

CLASSIFIER = {"id": "native", "version": "1"}


def owner_answering(statuses):
    calls = []

    def native(body, *, context, cancellation):
        calls.append(body["id"])
        status = statuses.pop(0)
        if status == "ok":
            return {**failure(body["id"], "ok", ""), "status": "ok", "complete": True, "usage": empty_usage()}
        return failure(body["id"], status, "fixture")

    owner = object.__new__(Owner)
    owner.store = SimpleNamespace(register_tool_registry=lambda registry, *, context: "fixture", request=native)
    owner.incomplete_type = type("SnapshotIncomplete", (Exception,), {})
    owner.registry_generations = {}
    owner.registry_guard = threading.Lock()
    owner.graph_misses = LRUDict(MAX_GRAPH_MISSES)
    return owner, calls


def prepare(frame_id, thread_ids=("thread-a", "thread-b")):
    return request(
        frame_id,
        operation="prepare_graph",
        view={"handle": f"lease-{frame_id}", "classifier": CLASSIFIER, "selectors": [], "attachments": []},
        thread_ids=list(thread_ids),
        roots=["/codex/sessions"],
        direct_paths=[],
        active_since_unix_ms=1,
        deadline_unix_ms=1000 + frame_id,
    )["snapshot"]["request"]


def test_owner_answers_a_repeat_of_a_missed_graph_without_native_work(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    owner, calls = owner_answering(["deadline", "deadline", "ok", "deadline"])

    assert owner.call(prepare(1), context(), object(), [])["status"] == "deadline"
    assert owner.call(prepare(2), context(), object(), [])["status"] == "deadline"
    assert calls == ["request-1"]

    assert owner.call(prepare(3, ("thread-c",)), context(), object(), [])["status"] == "deadline"
    assert calls == ["request-1", "request-3"]

    clock[0] += GRAPH_MISS_SECONDS
    assert owner.call(prepare(4), context(), object(), [])["status"] == "ok"
    assert owner.call(prepare(5), context(), object(), [])["status"] == "deadline"
    assert calls == ["request-1", "request-3", "request-4", "request-5"]
