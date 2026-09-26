import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from captain_hook.snapshots.client import (
    CORE_SCHEMA,
    HOST_SCHEMA,
    MAX_VIEW_ATTACHMENTS,
    AttachmentLimit,
    EvidenceIncomplete,
    GraphEvidenceExpired,
    GraphSources,
    Lease,
    RegisteredWarmState,
    RemoteSession,
    RootWarmState,
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
    session = RemoteSession(
        client, Lease(client, source), Path(source["canonical_path"]), source["classifier"]
    ).with_registered_sources(GraphSources(thread_ids=tuple(f"thread-{index}" for index in range(918))))

    assert len(session) == 1
    assert session.has_edit_to("src/**", subagents=False) is True
    assert session.activity_probe(waiting_tools=[], tool_registry_generation="fixture") is False
    assert all(request["view"]["attachments"] == [] for request in requests)


def test_deep_query_over_attachment_bound_is_incomplete_before_transport():
    client = SnapshotClient(lambda _: pytest.fail("overbound view reached transport"))
    client.bind_tool_registry({})
    source = description()
    session = RemoteSession(
        client, Lease(client, source), Path(source["canonical_path"]), source["classifier"]
    ).with_registered_sources(
        GraphSources(thread_ids=tuple(f"thread-{index}" for index in range(MAX_VIEW_ATTACHMENTS + 1)))
    )

    assert len(session.view()["attachments"]) == 0
    with pytest.raises(AttachmentLimit, match="registered transcript attachments exceed 1024"):
        session.has_edit_to("src/**")


def test_real_owner_reuses_prepared_graph(tmp_path, monkeypatch):
    from captain_hook.testing.snapshots import FixtureOwner
    from tests.helpers import raw_text
    from tests.test_snapshot_fixture_owner import write_messages

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    source = tmp_path / "root.jsonl"
    write_messages(source, raw_text("user", "root"))
    attachment_count = 9
    attachments = tuple(tmp_path / f"attachment-{index}.jsonl" for index in range(attachment_count))
    for path in attachments:
        write_messages(path, raw_text("user", "attached"))
    fixture = FixtureOwner()
    fixture.client.bind_tool_registry({})
    try:
        sources = GraphSources(direct_paths=attachments)
        cold = fixture.load(source).with_registered_sources(sources)
        before_cold = fixture.client.call("stats")["data"]["counters"]
        try:
            assert cold.has_edit_to("src/**") is False
        except EvidenceIncomplete as incomplete:
            assert incomplete.status in {"incomplete", "deadline"}
        after_cold = fixture.client.call("stats")["data"]["counters"]
        assert after_cold["source_bytes_read"] - before_cold["source_bytes_read"] <= 1024 * 1024
        cold.release()

        warmer = RegisteredWarmState(sources, fixture.client)
        fixture.context["work_class"] = "background"
        for _ in range(attachment_count // 8 + 16):
            if warmer.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3):
                break
        else:
            pytest.fail("registered fixture did not finish warming")
        fixture.context["work_class"] = "foreground"

        session = fixture.load(source).with_registered_sources(sources)
        requests = []
        exchange = fixture.client._exchange

        def record(request):
            requests.append(request["request"])
            return exchange(request)

        fixture.client._exchange = record
        before = fixture.client.call("stats")["data"]["counters"]
        session.graph.require(session)
        prepared = fixture.client.call("stats")["data"]["counters"]
        assert prepared["source_bytes_read"] == before["source_bytes_read"]
        assert session.has_edit_to("src/**") is False
        assert session.has_read("README.md") is False
        after = fixture.client.call("stats")["data"]["counters"]
        assert after["source_bytes_read"] == prepared["source_bytes_read"]
        assert after["nonincremental_lowering_calls"] == prepared["nonincremental_lowering_calls"]
        assert after["nonincremental_lowering_source_bytes"] == prepared["nonincremental_lowering_source_bytes"]
        graph_preparations = [request for request in requests if request["operation"] == "prepare_graph"]
        graph_queries = [request for request in requests if request["operation"] == "query_graph"]
        assert len(graph_preparations) == 1
        assert len(graph_queries) == 2
        assert graph_preparations[0]["direct_paths"] == [str(path) for path in attachments]
        assert all("view" not in request and "direct_paths" not in request for request in graph_queries)
        session.release()
        warm = fixture.load(source).with_registered_sources(sources)
        before_warm = fixture.client.call("stats")["data"]["counters"]
        warm.graph.require(warm)
        assert warm.has_edit_to("src/**") is False
        after_warm = fixture.client.call("stats")["data"]["counters"]
        assert after_warm["source_bytes_read"] == before_warm["source_bytes_read"]
        assert after_warm["nonincremental_lowering_calls"] == before_warm["nonincremental_lowering_calls"]
        warm.release()
    finally:
        fixture.close()


def test_registered_warming_reuses_facts_across_claimants_only_under_the_same_authority(tmp_path, monkeypatch):
    from captain_hook.testing.snapshots import FixtureOwner
    from tests.helpers import raw_text
    from tests.test_snapshot_fixture_owner import write_messages

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    source = tmp_path / "root.jsonl"
    attachment = tmp_path / "attachment.jsonl"
    write_messages(source, raw_text("user", "root"))
    write_messages(attachment, raw_text("user", "attached"))
    fixture = FixtureOwner()
    fixture.client.bind_tool_registry({})
    warm = RegisteredWarmState(GraphSources(direct_paths=(attachment,)), fixture.client)
    try:
        fixture.context["claimant"] = "operator"
        fixture.context["work_class"] = "background"
        assert warm.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is True
        fixture.context["claimant"] = "foreground"
        fixture.context["work_class"] = "foreground"
        session = fixture.load(source).with_registered_sources(GraphSources(direct_paths=(attachment,)))
        before = fixture.client.call("stats")["data"]["counters"]
        assert session.has_edit_to("src/**") is False
        after = fixture.client.call("stats")["data"]["counters"]
        assert after["source_bytes_read"] == before["source_bytes_read"]
        session.release()

        authority = fixture.context["authority"]
        fixture.context["authority"] = {
            "kind": "restricted_roots",
            "effective_uid": authority["effective_uid"],
            "roots": [str(tmp_path)],
        }
        fixture.context["claimant"] = "restricted"
        fixture.context["work_class"] = "background"
        restricted = RegisteredWarmState(GraphSources(direct_paths=(attachment,)), fixture.client)
        before_restricted = fixture.client.call("stats")["data"]["counters"]
        assert restricted.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3) is True
        after_restricted = fixture.client.call("stats")["data"]["counters"]
        assert after_restricted["source_bytes_read"] > before_restricted["source_bytes_read"]
    finally:
        fixture.close()


def test_native_root_warming_reuses_source_and_append_facts(tmp_path, monkeypatch):
    from captain_hook.testing.helpers import fixture_line
    from captain_hook.testing.snapshots import FixtureOwner
    from tests.helpers import raw_text
    from tests.test_snapshot_fixture_owner import write_messages

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    source = tmp_path / "root.jsonl"
    write_messages(source, raw_text("user", "x" * (1024 * 1024)))
    fixture = FixtureOwner()
    fixture.client.bind_tool_registry({})
    warm_client = fixture.client_for_context(work_class="background", claimant="warmer")
    warm_client.bind_tool_registry({})

    def warm_root(classifier):
        state = RootWarmState(source, classifier, warm_client)
        for _ in range(16):
            if state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3):
                return
        pytest.fail("root fixture did not finish warming")

    def foreground_p95():
        samples = []
        for _ in range(12):
            started = time.perf_counter()
            session = fixture.load(source)
            try:
                assert session.has_read("missing") is False
            finally:
                session.release()
            samples.append(time.perf_counter() - started)
        return sorted(samples)[11]

    try:
        warm_root({"id": "native", "version": "1"})
        first = fixture.load(source)
        classifier = first.classifier
        first.release()
        warm_root(classifier)
        before = fixture.client.call("stats")["data"]["counters"]
        warm_p95 = foreground_p95()
        after = fixture.client.call("stats")["data"]["counters"]
        assert after["source_bytes_read"] == before["source_bytes_read"]
        print(f"prepared root foreground p95_ms={warm_p95 * 1000:.1f}")
        assert warm_p95 < 0.75

        with source.open("a") as stream:
            stream.write(json.dumps(fixture_line(1, raw_text("user", "tail"))) + "\n")
        before_append = fixture.client.call("stats")["data"]["counters"]
        warm_root({"id": "native", "version": "1"})
        warm_root(classifier)
        append_p95 = foreground_p95()
        after_append = fixture.client.call("stats")["data"]["counters"]
        assert after_append["source_bytes_read"] - before_append["source_bytes_read"] < 512 * 1024
        print(f"prepared root append foreground p95_ms={append_p95 * 1000:.1f}")
        assert append_p95 < 0.75
    finally:
        warm_client.close()
        fixture.close()


def test_foreground_graph_latency_stays_bounded_while_registry_warms(tmp_path, monkeypatch):
    from captain_hook.testing.snapshots import FixtureOwner
    from tests.helpers import raw_text
    from tests.test_snapshot_fixture_owner import write_messages

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    source = tmp_path / "root.jsonl"
    write_messages(source, raw_text("user", "root"))
    attachments = tuple(tmp_path / f"attachment-{index}.jsonl" for index in range(923))
    for index, path in enumerate(attachments):
        write_messages(path, raw_text("user", "x" * (1024 * 1024 if index == 0 else 1024)))
    fixture = FixtureOwner()
    fixture.client.bind_tool_registry({})
    warm_client = fixture.client_for_context(work_class="background", claimant="warmer")
    warm_client.bind_tool_registry({})
    sources = GraphSources(direct_paths=attachments)
    start_together = threading.Barrier(2)

    def warm():
        start_together.wait()
        state = RegisteredWarmState(sources, warm_client)
        for _ in range(len(attachments) // 8 + 16):
            if state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3):
                return
        pytest.fail("registered fixture did not finish warming")

    try:
        samples = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(warm)
            start_together.wait()
            for _ in range(30):
                started = time.perf_counter()
                session = fixture.load(source).with_registered_sources(sources)
                try:
                    try:
                        assert session.has_edit_to("src/**") is False
                    except EvidenceIncomplete as exc:
                        assert exc.status in {"incomplete", "deadline"} or (
                            isinstance(exc, GraphEvidenceExpired) and exc.status in {"stale_cursor", "stale_handle"}
                        )
                finally:
                    session.release()
                samples.append(time.perf_counter() - started)
            future.result(timeout=30)
        p95 = sorted(samples)[28]
        print(f"prepared graph foreground p95_ms={p95 * 1000:.1f}")
        assert p95 < 1.0
        requests = []
        exchange = fixture.client._exchange

        def record(request):
            requests.append(request["request"]["operation"])
            return exchange(request)

        fixture.client._exchange = record
        before_warm_query = fixture.client.call("stats")["data"]["counters"]
        session = fixture.load(source).with_registered_sources(sources)
        try:
            assert session.has_edit_to("src/**") is False
            assert session.has_read("README.md") is False
        finally:
            session.release()
        after_warm_query = fixture.client.call("stats")["data"]["counters"]
        assert after_warm_query["source_bytes_read"] == before_warm_query["source_bytes_read"]
        assert requests.count("prepare_graph") == 1
        assert requests.count("query_graph") == 2
    finally:
        warm_client.close()
        fixture.close()


def test_codex_append_warming_does_not_lower_the_whole_source(tmp_path, monkeypatch):
    from captain_hook.testing.snapshots import FixtureOwner
    from tests.helpers import raw_text
    from tests.test_snapshot_fixture_owner import write_messages

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    source = tmp_path / "root.jsonl"
    active = tmp_path / "active.jsonl"
    write_messages(source, raw_text("user", "root"))
    metadata = {
        "timestamp": "2026-01-01T00:00:00Z",
        "type": "session_meta",
        "payload": {"id": "thread-append", "cwd": str(tmp_path), "originator": "codex_exec", "source": "exec"},
    }
    initial = {
        "timestamp": "2026-01-01T00:00:01Z",
        "type": "event_msg",
        "payload": {"type": "user_message", "message": "x" * (1024 * 1024)},
    }
    active.write_text(json.dumps(metadata) + "\n" + json.dumps(initial) + "\n")
    fixture = FixtureOwner()
    fixture.client.bind_tool_registry({})
    warm_client = fixture.client_for_context(work_class="background", claimant="warmer")
    warm_client.bind_tool_registry({})
    sources = GraphSources(direct_paths=(active,))
    state = RegisteredWarmState(sources, warm_client)

    try:
        for _ in range(16):
            if state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3):
                break
        else:
            pytest.fail("active source did not finish initial warming")
        before_append = fixture.client.call("stats")["data"]["counters"]
        appended_samples = []
        append_cpu_ms = []
        for index in range(12):
            with active.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "timestamp": f"2026-01-01T00:00:{index + 2:02}Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message" if index % 2 == 0 else "agent_message",
                                "message": f"new-{index}",
                            },
                        }
                    )
                    + "\n"
                )
            cpu_started = time.process_time()
            for _ in range(16):
                if state.step(read_bytes=8 * 1024 * 1024, deadline_seconds=3):
                    break
            else:
                pytest.fail("appended source did not finish warming")
            started = time.perf_counter()
            session = fixture.load(source).with_registered_sources(sources)
            try:
                assert session.has_edit_to("src/**") is False
            finally:
                session.release()
            appended_samples.append(time.perf_counter() - started)
            append_cpu_ms.append((time.process_time() - cpu_started) * 1000)
        after_append = fixture.client.call("stats")["data"]["counters"]
        append_p95 = sorted(appended_samples)[11]
        lowered_bytes = (
            after_append["nonincremental_lowering_source_bytes"] - before_append["nonincremental_lowering_source_bytes"]
        )
        source_bytes = after_append["source_bytes_read"] - before_append["source_bytes_read"]
        print(f"prepared graph warm append foreground p95_ms={append_p95 * 1000:.1f}")
        print(f"prepared graph warm append cpu_total_ms={sum(append_cpu_ms):.1f}")
        print(f"prepared graph warm append source_bytes_read={source_bytes}")
        print(f"prepared graph warm append nonincremental_lowering_source_bytes={lowered_bytes}")
        assert append_p95 < 1.0
        assert source_bytes < 2 * 1024 * 1024
        assert lowered_bytes < 1024 * 1024
    finally:
        warm_client.close()
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


def test_abandoned_foreground_cursor_waits_until_after_reply_for_cleanup():
    cleanup_started = threading.Event()
    finish_cleanup = threading.Event()

    def exchange(wrapper):
        return response(wrapper["request"], {"kind": "strings", "values": ["first"]}, cursor="cursor")

    def cleanup(wrapper):
        cleanup_started.set()
        assert finish_cleanup.wait(2)
        return response(wrapper["request"], {"kind": "released", "released": True})

    client = SnapshotClient(exchange, cleanup_exchange=cleanup, defer_cleanup=True)
    client.bind_tool_registry({})
    pages = client.pages("query", view={}, query={})
    assert next(pages) == {"kind": "strings", "values": ["first"]}
    pages.close()
    assert not cleanup_started.is_set()

    future = ThreadPoolExecutor(max_workers=1)
    try:
        settled = future.submit(client.close_pending)
        assert cleanup_started.wait(3)
        assert not settled.done()
        finish_cleanup.set()
        settled.result(timeout=3)
    finally:
        finish_cleanup.set()
        future.shutdown()


def test_foreground_budget_covers_root_acquire_and_classifier():
    requests = []

    def exchange(wrapper):
        request = wrapper["request"]
        requests.append(request)
        if request["operation"] == "acquire":
            result = response(request, {"kind": "acquired", "description": description()})
            result["response"]["usage"]["source_bytes_read"] = 700 * 1024
            return result
        if request["operation"] == "prepare_hook_view":
            result = response(request, {"kind": "classifier", "classifier": {"id": "native", "version": "1"}})
            result["response"]["usage"]["source_bytes_read"] = 300 * 1024
            return result
        result = response(request, {"kind": "scalar", "value": False})
        result["response"]["usage"]["source_bytes_read"] = 24 * 1024
        return result

    client = SnapshotClient(exchange, foreground_seconds=0.75, foreground_read_bytes=1024 * 1024)
    client.bind_tool_registry({})
    session = client.acquire("/tmp/fixture.jsonl")
    list(client.pages("prepare_hook_view", domain=True, view=session.view(), cwd="/tmp", droid=False))
    client.call("query", view=session.view(), query={"kind": "has_read", "pattern": "x", "subagents": False})
    with pytest.raises(EvidenceIncomplete, match="foreground transcript byte budget exhausted"):
        client.call("stats")

    assert [request["operation"] for request in requests] == ["acquire", "prepare_hook_view", "query"]
    assert [request["limits"]["max_read_bytes"] for request in requests] == [
        1024 * 1024,
        324 * 1024,
        24 * 1024,
    ]
    assert len({request["deadline_unix_ms"] for request in requests}) == 1
    assert requests[0]["deadline_unix_ms"] <= int(time.time() * 1000) + 750


def test_foreground_root_cursor_stops_before_a_second_read_step():
    operations = []

    def exchange(wrapper):
        request = wrapper["request"]
        operations.append(request["operation"])
        result = response(request, {"kind": "strings", "values": []}, cursor="root-cursor")
        result["response"]["usage"]["source_bytes_read"] = 1024 * 1024
        return result

    def cleanup(wrapper):
        return response(wrapper["request"], {"kind": "released", "released": True})

    client = SnapshotClient(
        exchange,
        cleanup_exchange=cleanup,
        defer_cleanup=True,
        foreground_seconds=0.75,
        foreground_read_bytes=1024 * 1024,
    )
    client.bind_tool_registry({})

    with pytest.raises(EvidenceIncomplete, match="foreground transcript byte budget exhausted"):
        list(client.pages("acquire", path="/tmp/large-root.jsonl", classifier={"id": "native", "version": "1"}))
    assert operations == ["acquire"]
    client.close_pending()


def test_expired_foreground_budget_never_sends_a_native_request():
    client = SnapshotClient(
        lambda _: pytest.fail("expired foreground request was sent"),
        foreground_seconds=0,
    )
    client.bind_tool_registry({})

    with pytest.raises(EvidenceIncomplete, match="foreground transcript deadline exhausted"):
        client.call("acquire", path="/tmp/root.jsonl", classifier={"id": "native", "version": "1"})


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
        session = fixture.load(source).with_registered_sources(GraphSources(direct_paths=(attachment,)))
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


def test_post_reply_cleanup_retries_only_deferred_leases():
    requests = []

    def cleanup(wrapper):
        request = wrapper["request"]
        requests.append(request)
        return response(request, {"kind": "released", "released": True})

    client = SnapshotClient(lambda _: pytest.fail("foreground exchange used"), cleanup_exchange=cleanup)
    client.bind_tool_registry({})
    active = Lease(client, description("active"))
    deferred = Lease(client, description("deferred"))
    deferred.cleanup_pending = True

    client.close_pending()

    assert not active.released
    assert deferred.released
    assert [request["token"] for request in requests] == ["deferred"]


def test_graph_query_budget_bounds_each_partial_step(tmp_path, monkeypatch):
    from captain_hook.snapshots.client import DEFAULT_LIMITS, EvidenceIncomplete
    from captain_hook.testing.snapshots import FixtureOwner
    from tests.helpers import raw_text
    from tests.test_snapshot_fixture_owner import write_messages

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    source = tmp_path / "root.jsonl"
    attachment = tmp_path / "attachment.jsonl"
    write_messages(source, raw_text("user", "root"))
    write_messages(attachment, raw_text("user", "x" * 4096))
    fixture = FixtureOwner()
    try:
        session = fixture.load(source).with_registered_sources(GraphSources(direct_paths=(attachment,)))
        monkeypatch.setitem(DEFAULT_LIMITS, "max_read_bytes", 256)
        before = fixture.client.call("stats")["data"]["counters"]
        with pytest.raises(EvidenceIncomplete) as first:
            session.has_edit_to("src/**")
        assert first.value.status in {"source_limit", "incomplete"}
        after_first = fixture.client.call("stats")["data"]["counters"]
        with pytest.raises(EvidenceIncomplete) as second:
            session.has_read("README.md")
        assert second.value.status in {"source_limit", "incomplete"}
        after_second = fixture.client.call("stats")["data"]["counters"]
        assert 0 <= after_first["source_bytes_read"] - before["source_bytes_read"] <= 256
        assert 0 <= after_second["source_bytes_read"] - after_first["source_bytes_read"] <= 256
        session.release()
    finally:
        fixture.close()
