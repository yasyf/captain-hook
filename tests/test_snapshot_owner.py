import hashlib
import io
import json
import re
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from captain_hook.snapshots.client import (
    CORE_SCHEMA,
    HOST_SCHEMA,
    MAX_FRAME_BYTES,
    Bridge,
    EvidenceIncomplete,
    SnapshotProtocolError,
    encode_frame,
    fails_open,
)
from captain_hook.snapshots.validation import checked, validator
from captain_hook.snapshots.worker import OWNER_ADMISSION, OwnerService, empty_usage, failure, handshake, read_frame


def context(admission="hook"):
    return {
        "claimant": "fixture",
        "admission": admission,
        "authority": {"kind": "user", "effective_uid": "501"},
        "registry_generation": "fixture",
    }


def request(frame_id=1, operation="stats", **fields):
    return {
        "protocol": 1,
        "op": "snapshot_request",
        "id": frame_id,
        "snapshot_context": context(),
        "snapshot": {
            "schema": HOST_SCHEMA,
            "tool_registry": [],
            "request": {"schema": CORE_SCHEMA, "id": f"request-{frame_id}", "operation": operation, **fields},
        },
    }


def wait_idle(service):
    deadline = time.monotonic() + 2
    while service.pending and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not service.pending


def test_owner_rejects_oversized_declared_frame_before_payload_read():
    stream = io.BytesIO(struct.pack(">I", MAX_FRAME_BYTES + 1))
    with pytest.raises(SnapshotProtocolError, match="encoded byte bound"):
        read_frame(stream)
    assert stream.tell() == 4


def test_owner_handshake_has_exact_build_and_config():
    config = {key: field["default"] for key, field in validator("config").schema["properties"].items()}
    hello = {"protocol": 1, "op": "hello", "build": "fixture", "snapshot_config": config}
    output = io.BytesIO()
    assert handshake(io.BytesIO(encode_frame(hello)), output, build="fixture") == config
    output.seek(0)
    assert read_frame(output) == {"protocol": 1, "op": "hello", "build": "fixture"}
    with pytest.raises(SnapshotProtocolError, match="exact build"):
        handshake(io.BytesIO(encode_frame(hello)), io.BytesIO(), build="other")


def test_warm_registered_uses_its_own_owner_lane_with_hook_cache_identity():
    calls = []
    owner = SimpleNamespace(
        token_type=threading.Event,
        call=lambda body, ctx, token, registry: (
            calls.append((body, ctx)) or failure(body["id"], "incomplete", "bounded fixture")
        ),
        close=lambda: None,
        record_transport=lambda count: None,
        discard=lambda response, ctx: None,
    )
    service = OwnerService(io.BytesIO(), io.BytesIO(), owner)
    for lane in ("hook", "review"):
        for _ in range(OWNER_ADMISSION[lane][1]):
            assert service.slots[lane].acquire(blocking=False)
    frame = request(
        operation="warm_registered",
        classifier={"id": "native", "version": "1"},
        thread_ids=["thread"],
        roots=["/tmp"],
        direct_paths=[],
        start_index=0,
        membership_revision=None,
        deadline_unix_ms=9_000_000_000_000,
        limits={
            "max_read_bytes": 8 * 1024 * 1024,
            "max_source_read_bytes": 8 * 1024 * 1024,
            "max_events": 1_000_000,
            "max_items": 65_536,
            "max_output_bytes": 16 * 1024 * 1024,
            "max_discovery_entries": 50_000,
            "max_sources": 4096,
        },
    )
    try:
        service.submit(frame)
        wait_idle(service)
        assert len(calls) == 1
        assert calls[0][0]["operation"] == "warm_registered"
        assert calls[0][1]["admission"] == "hook"
        assert not service.slots["hook"].acquire(blocking=False)
        assert not service.slots["review"].acquire(blocking=False)
        assert service.slots["warm"].acquire(blocking=False)
    finally:
        for executor in service.executors.values():
            executor.shutdown()


@pytest.mark.parametrize(
    "lexeme,accepted",
    [
        ("1", True),
        ("1.0", True),
        ("1e0", True),
        ("1.00000000000000001", False),
        ("9007199254740990.5", False),
        ("9007199254740991", True),
        ("1e100000000", False),
        ("1e-100000000", False),
    ],
)
def test_actual_owner_ingress_preserves_numeric_lexemes(lexeme, accepted):
    calls = []
    owner = SimpleNamespace(
        token_type=threading.Event,
        call=lambda body, ctx, token, registry: calls.append(body) or failure(body["id"], "missing", "fixture"),
        close=lambda: None,
        record_transport=lambda count: None,
        discard=lambda response, ctx: None,
    )
    output = io.BytesIO()
    service = OwnerService(io.BytesIO(), output, owner)
    body = request(
        operation="acquire",
        path="/tmp/fixture.jsonl",
        classifier={"id": "native", "version": "1"},
        deadline_unix_ms=12345,
        limits={
            "max_read_bytes": 1,
            "max_source_read_bytes": 1,
            "max_events": 1,
            "max_items": 1,
            "max_output_bytes": 1,
            "max_discovery_entries": 1,
            "max_sources": 1,
        },
    )
    raw = json.dumps(body).replace('"deadline_unix_ms": 12345', f'"deadline_unix_ms": {lexeme}').encode()
    frame = read_frame(io.BytesIO(struct.pack(">I", len(raw)) + raw))
    try:
        if accepted:
            service.submit(frame)
            wait_idle(service)
            assert type(calls[0]["deadline_unix_ms"]) is int
        else:
            with pytest.raises((ValidationError, ValueError)):
                service.submit(frame)
            assert calls == []
    finally:
        for executor in service.executors.values():
            executor.shutdown()


def test_outer_frame_id_keeps_lexical_integer_contract():
    service = OwnerService(
        io.BytesIO(),
        io.BytesIO(),
        SimpleNamespace(record_transport=lambda count: None, discard=lambda response, ctx: None),
    )
    raw = json.dumps(request()).replace('"id": 1,', '"id": 1.0,').encode()
    try:
        with pytest.raises(SnapshotProtocolError, match="identity"):
            service.submit(read_frame(io.BytesIO(struct.pack(">I", len(raw)) + raw)))
    finally:
        for executor in service.executors.values():
            executor.shutdown()


def test_whole_envelope_overflow_returns_bounded_failure_then_connection_reused():
    output = io.BytesIO()
    service = OwnerService(
        io.BytesIO(), output, SimpleNamespace(record_transport=lambda count: None, discard=lambda response, ctx: None)
    )
    response = {
        "schema": CORE_SCHEMA,
        "id": "fixture",
        "status": "ok",
        "complete": True,
        "data": {"kind": "strings", "values": ["x" * 600_000, "y" * 600_000]},
        "cursor": None,
        "reason": None,
        "usage": empty_usage(),
    }
    try:
        service.result(1, response, context())
        service.result(2, failure("fixture-2", "missing", "still alive"), context())
        output.seek(0)
        first = read_frame(output)
        second = read_frame(output)
        checked("host-response", first["snapshot"])
        assert first["snapshot"]["response"]["status"] == "output_limit"
        assert second["id"] == 2
    finally:
        for executor in service.executors.values():
            executor.shutdown()


def test_cancel_one_waiter_and_release_work_do_not_cancel_other_hook():
    started = threading.Barrier(3)
    proceed = threading.Event()
    tokens = {}

    class Token:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    def call(body, ctx, token, registry):
        tokens[body["id"]] = token
        if body["operation"] != "release":
            started.wait(timeout=2)
            assert proceed.wait(timeout=2)
        return failure(body["id"], "cancelled" if token.cancelled else "missing", "fixture")

    service = OwnerService(
        io.BytesIO(),
        io.BytesIO(),
        SimpleNamespace(
            token_type=Token,
            call=call,
            close=lambda: None,
            record_transport=lambda count: None,
            discard=lambda response, ctx: None,
        ),
    )
    try:
        service.submit(request(1))
        service.submit(request(2))
        started.wait(timeout=2)
        service.submit({"protocol": 1, "op": "snapshot_cancel", "id": 1})
        service.submit(request(3, "release", owner_epoch="owner", kind="lease", token="lease"))
        deadline = time.monotonic() + 2
        while "request-3" not in tokens and time.monotonic() < deadline:
            time.sleep(0.005)
        assert "request-3" in tokens
        assert tokens["request-1"].cancelled
        assert not tokens["request-2"].cancelled
        proceed.set()
        wait_idle(service)
    finally:
        proceed.set()
        for executor in service.executors.values():
            executor.shutdown()


def test_async_cancelled_policy_sends_terminal_reply_and_preserves_other_requests():
    import asyncio

    def call(body, ctx, token, registry):
        if body["id"] == "request-1":
            raise asyncio.CancelledError()
        return failure(body["id"], "missing", "fixture")

    output = io.BytesIO()
    owner = SimpleNamespace(
        token_type=threading.Event,
        call=call,
        close=lambda: None,
        record_transport=lambda count: None,
        discard=lambda response, ctx: None,
    )
    service = OwnerService(io.BytesIO(), output, owner)
    try:
        service.submit(request(1))
        wait_idle(service)
        service.submit(request(2))
        wait_idle(service)
        output.seek(0)
        assert read_frame(output)["snapshot"]["response"]["status"] == "cancelled"
        assert read_frame(output)["snapshot"]["response"]["status"] == "missing"
    finally:
        for executor in service.executors.values():
            executor.shutdown()


def test_saturated_owner_returns_bounded_failure_without_submitting():
    output = io.BytesIO()
    owner = SimpleNamespace(record_transport=lambda count: None, discard=lambda response, ctx: None)
    service = OwnerService(io.BytesIO(), output, owner)
    for _ in range(OWNER_ADMISSION["hook"][1]):
        assert service.slots["hook"].acquire(blocking=False)
    try:
        service.submit(request())
        output.seek(0)
        assert read_frame(output)["snapshot"]["response"]["status"] == "retained_limit"
        assert not service.pending
    finally:
        for executor in service.executors.values():
            executor.shutdown()


def test_saturated_release_keeps_hook_ingress_and_cancellation_live():
    started = threading.Event()
    proceed = threading.Event()
    tokens = {}

    class Token:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    def call(body, ctx, token, registry):
        tokens[body["id"]] = token
        started.set()
        proceed.wait(timeout=2)
        return failure(body["id"], "cancelled" if token.cancelled else "missing", "fixture")

    output = io.BytesIO()
    owner = SimpleNamespace(
        token_type=Token,
        call=call,
        close=lambda: None,
        record_transport=lambda count: None,
        discard=lambda response, ctx: None,
    )
    service = OwnerService(io.BytesIO(), output, owner)
    slots = OWNER_ADMISSION["release"][1]
    for _ in range(slots):
        assert service.slots["release"].acquire(blocking=False)
    try:
        service.submit(request(1, "release", owner_epoch="owner", kind="lease", token="lease"))
        service.submit(request(2))
        assert started.wait(timeout=2)
        service.submit({"protocol": 1, "op": "snapshot_cancel", "id": 2})
        assert tokens["request-2"].cancelled
        proceed.set()
        wait_idle(service)
        output.seek(0)
        assert [read_frame(output)["snapshot"]["response"]["status"] for _ in range(2)] == [
            "retained_limit",
            "cancelled",
        ]
    finally:
        proceed.set()
        for _ in range(slots):
            service.slots["release"].release()
        for executor in service.executors.values():
            executor.shutdown()


def test_owner_queues_burst_without_starting_unbounded_work():
    started = 0
    guard = threading.Lock()
    release = threading.Event()

    def call(body, ctx, token, registry):
        nonlocal started
        with guard:
            started += 1
        release.wait(timeout=2)
        return failure(body["id"], "missing", "fixture")

    output = io.BytesIO()
    owner = SimpleNamespace(
        token_type=threading.Event,
        call=call,
        close=lambda: None,
        record_transport=lambda count: None,
        discard=lambda response, ctx: None,
    )
    service = OwnerService(io.BytesIO(), output, owner)
    workers = OWNER_ADMISSION["hook"][0]
    try:
        for frame_id in range(1, workers + 2):
            service.submit(request(frame_id))
        deadline = time.monotonic() + 2
        while started < workers and time.monotonic() < deadline:
            time.sleep(0.005)
        assert started == workers
        assert len(service.pending) == workers + 1
        assert output.getvalue() == b""
        release.set()
        wait_idle(service)
        output.seek(0)
        assert [read_frame(output)["snapshot"]["response"]["status"] for _ in range(workers + 1)] == ["missing"] * (
            workers + 1
        )
    finally:
        release.set()
        for executor in service.executors.values():
            executor.shutdown()


def test_rejected_response_discards_its_native_resources():
    discarded = []
    owner = SimpleNamespace(
        record_transport=lambda count: None, discard=lambda response, ctx: discarded.append((response, ctx))
    )
    service = OwnerService(io.BytesIO(), io.BytesIO(), owner)
    response = {
        "schema": CORE_SCHEMA,
        "id": "fixture",
        "status": "ok",
        "complete": True,
        "data": {"kind": "strings", "values": ["x" * 600_000, "y" * 600_000]},
        "cursor": None,
        "reason": None,
        "usage": empty_usage(),
    }
    try:
        service.result(1, response, context())
        assert discarded == [(response, context())]
        with pytest.raises(ValidationError):
            service.result(2, {"not": "valid"}, context())
        assert discarded[-1] == ({"not": "valid"}, context())
    finally:
        for executor in service.executors.values():
            executor.shutdown()


def test_exact_zero_is_canonicalized_without_expanding_exponent():
    from captain_hook.snapshots.validation import canonical_numbers, parse_exact

    assert canonical_numbers(parse_exact(b'{"zero": 0e100000000, "negative_zero": -0e-100000000}')) == {
        "zero": 0,
        "negative_zero": 0,
    }


@pytest.mark.parametrize(
    "path,cwd,droid,facts,expected",
    [
        ("/tmp/session/subagents/agent-a.jsonl", None, True, {}, "captain-lane"),
        (
            "/tmp/session.jsonl",
            "/tmp/conductor/workspaces/project",
            True,
            {"has_users": True, "all_users_sidechain": True, "has_user_prefix": True},
            "captain-lane",
        ),
        (
            "/tmp/session.jsonl",
            "/tmp/conductor/workspaces/project",
            True,
            {"has_users": True, "all_users_sidechain": False, "has_user_prefix": True},
            "native",
        ),
        (
            "/tmp/session.jsonl",
            None,
            False,
            {"has_users": True, "all_users_sidechain": False, "has_user_prefix": True},
            "captain-conductor",
        ),
        (
            "/tmp/session.jsonl",
            None,
            False,
            {"has_users": False, "all_users_sidechain": True, "has_user_prefix": False},
            "native",
        ),
    ],
)
def test_builtin_classifier_priority_uses_cached_native_facts(path, cwd, droid, facts, expected):
    from captain_hook.snapshots.worker import Owner

    calls = []

    def classifier_facts(prefix, *, event_limit):
        calls.append((prefix, event_limit))
        return facts

    snapshot = SimpleNamespace(description={"canonical_path": path}, classifier_facts=classifier_facts)
    owner = object.__new__(Owner)
    assert owner._hook_classifier(snapshot, {"cwd": cwd, "droid": droid}) == {"id": expected, "version": "1"}
    assert calls == ([] if not facts else [("<system_instruction>", 50)])


def test_owner_binds_registry_content_without_changing_authority():
    from captain_hook.snapshots.worker import Owner

    captured = []
    specs = [{"name": "custom_edit", "behaves_like": "Edit", "span_edit": None}]

    def register(registry, *, context):
        assert context["admission"] == "hook"
        assert registry == specs
        return "content-fingerprint"

    def native_call(body, *, context, cancellation):
        captured.append(dict(context))
        return failure(body["id"], "missing", "fixture")

    owner = object.__new__(Owner)
    owner.store = SimpleNamespace(register_tool_registry=register, request=native_call)
    owner.incomplete_type = type("SnapshotIncomplete", (Exception,), {})
    owner.registry_generations = {}
    owner.registry_guard = threading.Lock()
    scope = context()
    authority = scope["authority"].copy()
    owner.call(request()["snapshot"]["request"], scope, object(), specs)
    assert captured[0]["registry_generation"] == "content-fingerprint"
    assert captured[0]["authority"] == authority
    assert captured[0]["claimant"] == "fixture"


def owner_with(native_call):
    from cc_transcript.snapshots import SnapshotIncomplete

    from captain_hook.snapshots.worker import Owner

    owner = object.__new__(Owner)
    owner.store = SimpleNamespace(register_tool_registry=lambda registry, *, context: "fixture", request=native_call)
    owner.incomplete_type = SnapshotIncomplete
    owner.registry_generations = {}
    owner.registry_guard = threading.Lock()
    return owner


@pytest.mark.parametrize("counter", ["source_bytes_read", "prepared_cache_bytes_read"])
@pytest.mark.parametrize("status", ["ok", "incomplete"])
def test_owner_material_read_logs_exact_response_usage_without_payload(logcap, counter, status):
    import os

    usage = empty_usage() | {counter: 4096, "discovery_entries_examined": 3}
    response = failure("request-1", status, "private response reason", usage)
    body = request(
        operation="query_graph",
        query={"kind": "has_tool", "pattern": "private tool pattern"},
        observers=[{"kind": "has_override", "texts": ["private override text"]}],
        selectors=[{"kind": "last_tool_call", "name": "private selector"}],
        path="/Users/private/transcript.jsonl",
    )["snapshot"]["request"]
    owner = owner_with(lambda *_, **__: response)

    assert owner.call(body, context(), object(), []) == response

    [record] = logcap.records
    assert record.levelno == 20
    assert "snapshot request completed: operation='query_graph' request_id='request-1'" in record.message
    assert f"pid={os.getpid()} status={status!r}" in record.message
    assert "query_kind='has_tool' observer_kinds=('has_override',) selector_count=1" in record.message
    assert "execution_path='core_request' cursor_in_sha256=None cursor_out_sha256=None" in record.message
    for key in (
        "source_opens",
        "source_bytes_read",
        "bytes_decoded",
        "events_parsed",
        "cold_parses",
        "append_parses",
        "discovery_entries_examined",
        "prepared_cache_reads",
        "prepared_cache_bytes_read",
    ):
        assert f"{key}={usage[key]}" in record.message
    for forbidden in ("private", "resource_sha256", "response reason", "max_read_bytes"):
        assert forbidden not in record.message


@pytest.mark.parametrize("owned", [False, True], ids=["core-fresh", "owned-producer"])
def test_owner_resume_distinguishes_identical_core_and_owned_usage(logcap, owned):
    cursor = "private consumed cursor"
    next_cursor = "private returned cursor"
    response = failure("request-1", "incomplete", "private reason", {"source_bytes_read": 4096, "cold_parses": 2})
    response["cursor"] = next_cursor
    page = {
        "field": "candidates_json",
        "records_json": ["private transcript payload"],
        "metadata": {"kind": "review"},
        "complete": False,
        "cursor": next_cursor,
        "usage": response["usage"],
    }

    def native_call(*_, **__):
        assert not owned
        return response

    owner = owner_with(native_call)
    owner.store.resume_projection = lambda *_, **__: page if owned else None
    body = request(operation="resume", cursor=cursor)["snapshot"]["request"]

    result = owner.call(body, context(), object(), [])

    assert result == (owner._page(page, "request-1") if owned else response)
    assert result["usage"] is response["usage"]
    [record] = logcap.records
    path = "owned_projection" if owned else "core_request"
    assert f"execution_path={path!r}" in record.message
    assert "source_bytes_read=4096" in record.message
    assert "cold_parses=2" in record.message
    assert f"cursor_in_sha256='{hashlib.sha256(cursor.encode()).hexdigest()}'" in record.message
    assert f"cursor_out_sha256='{hashlib.sha256(next_cursor.encode()).hexdigest()}'" in record.message
    assert "private" not in record.message
    assert "execution_path" not in result
    assert "cursor_in_sha256" not in result
    assert "cursor_out_sha256" not in result


def test_owner_zero_byte_cursor_origin_link_and_terminal_are_logged(logcap):
    cursors = ("private first cursor", "private second cursor")
    responses = [
        {
            "schema": CORE_SCHEMA,
            "id": f"request-{index}",
            "status": "incomplete" if cursor is not None else "ok",
            "complete": cursor is None,
            "data": None,
            "cursor": cursor,
            "reason": None,
            "usage": empty_usage(),
        }
        for index, cursor in enumerate((*cursors, None), 1)
    ]
    pending = iter(responses)
    owner = owner_with(lambda *_, **__: next(pending))
    owner.store.resume_projection = lambda *_, **__: None
    bodies = [
        request(operation="query_graph", query={"kind": "has_skill", "values": ["private skill"]}),
        request(frame_id=2, operation="resume", cursor=cursors[0]),
        request(frame_id=3, operation="resume", cursor=cursors[1]),
    ]

    results = [owner.call(body["snapshot"]["request"], context(), object(), []) for body in bodies]

    assert results == responses
    assert all(result["usage"] is response["usage"] for result, response in zip(results, responses, strict=True))
    assert len(logcap.records) == 3
    origin, link, terminal = [record.message for record in logcap.records]
    first, second = [hashlib.sha256(cursor.encode()).hexdigest() for cursor in cursors]
    assert "operation='query_graph'" in origin
    assert "query_kind='has_skill' observer_kinds=() selector_count=0" in origin
    assert f"cursor_in_sha256=None cursor_out_sha256='{first}'" in origin
    assert f"cursor_in_sha256='{first}' cursor_out_sha256='{second}'" in link
    assert f"cursor_in_sha256='{second}' cursor_out_sha256=None" in terminal
    for record in logcap.records:
        assert "execution_path='core_request'" in record.message
        assert "source_bytes_read=0" in record.message
        assert "prepared_cache_bytes_read=0" in record.message
        assert "private" not in record.message


def test_owner_completion_preserves_response_identity(logcap):
    owner = owner_with(lambda *_, **__: pytest.fail("native store was called"))
    body = request(operation="resume", cursor="private cursor")["snapshot"]["request"]
    response = failure("request-1", "ok", "private reason")
    before = json.dumps(response, sort_keys=True)

    assert owner._completed(body, response, time.monotonic(), execution_path="core_request") is response
    assert json.dumps(response, sort_keys=True) == before
    [record] = logcap.records
    assert "private" not in record.message


@pytest.mark.parametrize("examined", [0, 1_000_000])
def test_owner_zero_payload_reads_skip_log_metadata_work(logcap, examined):
    class Request(dict):
        def get(self, key, default=None):
            if key == "cursor":
                return super().get(key, default)
            pytest.fail(f"zero-read request examined logging metadata: {key}")

    body = Request(request(operation="discover")["snapshot"]["request"])
    response = failure("request-1", "ok", "fixture", {"discovery_entries_examined": examined})

    assert owner_with(lambda *_, **__: response).call(body, context(), object(), []) == response
    assert logcap.records == []


@pytest.mark.parametrize("operation", ["acquire", "resume"])
def test_owner_failed_read_retains_one_failure_record(logcap, operation):
    from cc_transcript.snapshots import SnapshotIncomplete

    def native_call(*_, **__):
        raise SnapshotIncomplete("cancelled", "private reason", usage={"source_bytes_read": 4096}, work={})

    fields = {"cursor": "private consumed cursor"} if operation == "resume" else {}
    body = request(operation=operation, **fields)["snapshot"]["request"]
    owner = owner_with(native_call)
    owner.store.resume_projection = lambda *_, **__: None
    response = owner.call(body, context(), object(), [])

    assert response["status"] == "cancelled"
    [record] = logcap.records
    assert "snapshot request failed:" in record.message
    assert "source_bytes_read=4096" in record.message
    assert "execution_path='unknown_error'" in record.message
    digest = hashlib.sha256(fields["cursor"].encode()).hexdigest() if fields else None
    assert f"cursor_in_sha256={digest!r} cursor_out_sha256=None" in record.message
    assert "private reason" not in record.message
    assert "private consumed cursor" not in record.message


def test_owner_missing_failure_renders_safe_context_through_its_message(logcap):
    from cc_transcript.snapshots import SnapshotIncomplete

    private = "/Users/fixture-user/private/session.jsonl"
    reason = "No such file or directory (os error 2) sk-fixture-secret"
    usage = {"source_opens": 1, "discovery_entries_examined": 3}

    def native_call(body, *, context, cancellation):
        raise SnapshotIncomplete("missing", reason, usage=usage, work={"max_read_bytes": 7})

    body = request(
        operation="acquire", path=private, classifier={"id": "native", "version": "1"}, deadline_unix_ms=12_345
    )["snapshot"]["request"]

    response = owner_with(native_call).call(body, context(), object(), [])

    assert response == failure("request-1", "missing", reason, usage)
    checked("response", response)
    digest = hashlib.sha256(json.dumps({"path": private}, sort_keys=True).encode()).hexdigest()[:16]
    [record] = logcap.records
    assert record.levelno == 20
    assert re.fullmatch(
        "INFO captain_hook\\.snapshots\\.worker: snapshot request failed: operation='acquire' request_id='request-1' "
        f"deadline_unix_ms=12345 resource_kind='path' resource_count=1 resource_sha256='{digest}' "
        "status='missing' elapsed_ms=\\d+\\.\\d discovery_entries_examined=3 source_opens=1 "
        "execution_path='unknown_error' cursor_in_sha256=None cursor_out_sha256=None\n",
        logcap.text,
    )
    for forbidden in (private, "os error", "sk-fixture-secret", "native", "max_read_bytes"):
        assert forbidden not in logcap.text


def test_owner_failure_without_counters_renders_them_unknown_and_no_handle(logcap):
    body = {
        "schema": HOST_SCHEMA,
        "id": "request-2",
        "operation": "prepare_review",
        "policy": {"id": "other", "version": "1"},
        "view": {"handle": {"owner_epoch": "owner", "snapshot_id": "snapshot", "lease_id": "lease-fixture-secret"}},
    }

    response = owner_with(lambda *_, **__: pytest.fail("native store was called")).call(body, context(), object(), [])

    assert response == failure("request-2", "invalid_request", "unregistered Captain evidence policy")
    assert re.fullmatch(
        "INFO captain_hook\\.snapshots\\.worker: snapshot request failed: operation='prepare_review' "
        "request_id='request-2' deadline_unix_ms=None resource_kind='view' resource_count=None resource_sha256=None "
        "status='invalid_request' elapsed_ms=\\d+\\.\\d usage='unknown' "
        "execution_path='unknown_error' cursor_in_sha256=None cursor_out_sha256=None\n",
        logcap.text,
    )
    assert "lease-fixture-secret" not in logcap.text
    assert "owner_epoch" not in logcap.text


REFUSING_HOST = """
import json, struct, sys
def read():
    (size,) = struct.unpack(">I", sys.stdin.buffer.read(4))
    return json.loads(sys.stdin.buffer.read(size))
def write(value):
    body = json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
    sys.stdout.buffer.flush()
write(read())
frame = read()
write({"protocol": 1, "op": "error", "id": frame["id"], "error": "captain: snapshot admission queue exhausted"})
"""


ECHOING_HOST = """
import json, struct, sys
def read():
    (size,) = struct.unpack(">I", sys.stdin.buffer.read(4))
    return json.loads(sys.stdin.buffer.read(size))
def write(value):
    body = json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
    sys.stdout.buffer.flush()
write(read())
frame = read()
write({"protocol": 1, "op": "snapshot_result", "id": frame["id"], "snapshot": {"parent_id": frame.get("parent_id")}})
"""


@pytest.mark.parametrize(("event", "parent"), [pytest.param(0, None, id="no_event"), pytest.param(7, 7, id="event")])
def test_the_bridge_sends_an_events_requests_under_the_event(event, parent):
    bridge = Bridge((sys.executable, "-c", ECHOING_HOST), event=event)
    assert bridge({"schema": HOST_SCHEMA}) == {"parent_id": parent}
    bridge.close()


@pytest.mark.parametrize(
    ("deadline_in_seconds", "status", "reason"),
    [
        pytest.param(None, "cancelled", "snapshot host failed the request: captain: snapshot admission", id="refused"),
        pytest.param(60, "cancelled", "snapshot host failed the request: captain: snapshot admission", id="in_time"),
        pytest.param(-1, "deadline", "snapshot transport deadline elapsed", id="past_its_deadline"),
    ],
)
def test_the_bridge_fails_open_on_a_request_the_host_did_not_serve(deadline_in_seconds, status, reason):
    request = {"schema": CORE_SCHEMA, "operation": "query_graph", "id": "one"}
    if deadline_in_seconds is not None:
        request["deadline_unix_ms"] = int((time.time() + deadline_in_seconds) * 1000)
    bridge = Bridge((sys.executable, "-c", REFUSING_HOST))
    with pytest.raises(EvidenceIncomplete, match=reason) as raised:
        bridge({"schema": HOST_SCHEMA, "request": request})
    bridge.close()
    assert raised.value.status == status
    assert fails_open(raised.value)
