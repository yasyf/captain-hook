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
    SnapshotProtocolError,
    encode_frame,
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
        "status='missing' elapsed_ms=\\d+\\.\\d discovery_entries_examined=3 source_opens=1\n",
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
        "status='invalid_request' elapsed_ms=\\d+\\.\\d usage='unknown'\n",
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


def test_the_bridge_names_the_error_the_host_returns():
    bridge = Bridge((sys.executable, "-c", REFUSING_HOST))
    with pytest.raises(SnapshotProtocolError, match="snapshot host failed the request: captain: snapshot admission"):
        bridge({"schema": HOST_SCHEMA})
    bridge.close()
