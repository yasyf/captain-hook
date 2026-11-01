from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

NODE = "tests/test_snapshot_client.py::test_foreground_graph_latency_stays_bounded_while_registry_warms"


@pytest.fixture(autouse=True)
def snapshot_latency_phase_diagnostic(request, monkeypatch):
    if request.node.nodeid != NODE:
        yield
        return

    from cc_transcript import snapshots as sdk

    from captain_hook.snapshots import client, validation, worker
    from captain_hook.testing import snapshots as fixtures

    records = []
    rounds = []
    dropped = 0
    guard = threading.Lock()
    local = threading.local()
    validator_cache_before = validation.validator.cache_info()._asdict()

    def append(row):
        nonlocal dropped
        with guard:
            if len(records) < 4096:
                records.append(row)
            else:
                dropped += 1

    def phase(name, function, *args, **kwargs):
        started = time.perf_counter_ns()
        try:
            return function(*args, **kwargs)
        finally:
            elapsed = time.perf_counter_ns() - started
            if (current := getattr(local, "client_call", None)) is not None:
                metric = current["phases"].setdefault(name, {"calls": 0, "elapsed_ns": 0, "max_ns": 0})
                metric["calls"] += 1
                metric["elapsed_ns"] += elapsed
                metric["max_ns"] = max(metric["max_ns"], elapsed)

    def wrap(original, layer, operation):
        def observed(instance, *args, **kwargs):
            started = time.perf_counter_ns()
            row = {
                "layer": layer,
                "operation": operation(args),
                "thread": threading.current_thread().name,
                "started_ns": started,
            }
            previous = getattr(local, "client_call", None)
            if layer == "client":
                row["phases"] = {}
                local.client_call = row
            if layer == "owner":
                row["work_class"] = "background" if args[1].get("work_class") == "background" else "foreground"
            response = None
            try:
                response = original(instance, *args, **kwargs)
                return response
            except BaseException as exc:
                row["error_type"] = type(exc).__name__
                raise
            finally:
                finished = time.perf_counter_ns()
                row.update(finished_ns=finished, elapsed_ns=finished - started)
                if layer in {"owner", "client"} and isinstance(response, dict):
                    row["status"] = response.get("status")
                    row["usage"] = {
                        key: value for key, value in response.get("usage", {}).items() if type(value) is int
                    }
                if layer == "client":
                    local.client_call = previous
                append(row)

        return observed

    def checked(name, value):
        label = name if name in {"host-request", "host-response", "context"} else "other"
        precise = phase(label + ".numeric_canonical_copy", validation.canonical_numbers, value)
        phase(label + ".python_schema_validate", validation.validate, name, precise)
        return phase(label + ".numeric_native_copy", validation.native_numbers, precise)

    for module in (fixtures, worker, validation):
        monkeypatch.setattr(module, "checked", checked)

    original_json = sdk._json
    original_call = sdk._call

    def sdk_json(value):
        return phase("sdk.input_wire_and_json", original_json, value)

    def sdk_call(function, *args):
        name = getattr(function, "__name__", "other")
        if name not in {"request", "resume_projection", "register_tool_registry", "discard_response"}:
            name = "other"

        def native(*values):
            return phase("native." + name + ".call_and_gil_return", function, *values)

        return original_call(native, *args)

    def sdk_request(self, request, *, cancellation, context):
        return phase(
            "sdk.response_json_decode",
            sdk.orjson.loads,
            sdk._call(self._native.request, sdk._json(request), sdk._json(context), cancellation._native),
        )

    def sdk_resume_projection(self, cursor, *, context, cancellation):
        result = sdk._call(self._native.resume_projection, cursor, sdk._json(context), cancellation._native)
        return None if result is None else phase("sdk.response_json_decode", sdk.orjson.loads, result)

    monkeypatch.setattr(sdk, "_json", sdk_json)
    monkeypatch.setattr(sdk, "_call", sdk_call)
    monkeypatch.setattr(sdk.TranscriptStore, "request", sdk_request)
    monkeypatch.setattr(sdk.TranscriptStore, "resume_projection", sdk_resume_projection)

    original_completed = worker.Owner._completed
    original_registry_generation = worker.Owner.registry_generation
    original_worker_metadata = worker.request_metadata
    original_client_metadata = client.request_metadata
    logger_type = type(worker.logger)
    original_info = logger_type.info

    def completed(self, *args, **kwargs):
        return phase("owner.completion_total", original_completed, self, *args, **kwargs)

    def registry_generation(self, *args, **kwargs):
        return phase("owner.registry_generation", original_registry_generation, self, *args, **kwargs)

    def worker_metadata(value):
        return phase("owner.resource_metadata_and_hash", original_worker_metadata, value)

    def client_metadata(value):
        return phase("client.resource_metadata_and_hash", original_client_metadata, value)

    def info(self, *args, **kwargs):
        return phase("logging.info_total", original_info, self, *args, **kwargs)

    monkeypatch.setattr(worker.Owner, "_completed", completed)
    monkeypatch.setattr(worker.Owner, "registry_generation", registry_generation)
    monkeypatch.setattr(worker, "request_metadata", worker_metadata)
    monkeypatch.setattr(client, "request_metadata", client_metadata)
    monkeypatch.setattr(logger_type, "info", info)

    bindings = [
        (fixtures.FixtureOwner, "load", "acquisition", lambda args: "load"),
        (client.RemoteSession, "has_edit_to", "query", lambda args: "has_edit_to"),
        (client.RemoteSession, "release", "release", lambda args: "release"),
        (client.SnapshotClient, "call", "client", lambda args: args[0]),
        (worker.Owner, "call", "owner", lambda args: args[0]["operation"]),
    ]
    for cls, name, layer, operation in bindings:
        monkeypatch.setattr(cls, name, wrap(getattr(cls, name), layer, operation))

    original_load = fixtures.FixtureOwner.load
    original_release = client.RemoteSession.release

    def load(self, *args, **kwargs):
        local.logical_round = {
            "index": len(rounds),
            "thread": threading.current_thread().name,
            "started_ns": time.perf_counter_ns(),
        }
        return original_load(self, *args, **kwargs)

    def release(self, *args, **kwargs):
        try:
            return original_release(self, *args, **kwargs)
        finally:
            if (row := getattr(local, "logical_round", None)) is not None:
                finished = time.perf_counter_ns()
                row.update(finished_ns=finished, elapsed_ns=finished - row["started_ns"])
                rounds.append(row)
                local.logical_round = None

    monkeypatch.setattr(fixtures.FixtureOwner, "load", load)
    monkeypatch.setattr(client.RemoteSession, "release", release)

    try:
        yield
    finally:
        distribution = importlib.metadata.distribution("cc-transcript")
        native = {}
        for entry in distribution.files or ():
            if entry.suffix in {".so", ".dylib", ".pyd"}:
                path = distribution.locate_file(entry)
                with path.open("rb") as stream:
                    payload = stream.read(64 * 1024 * 1024 + 1)
                assert len(payload) <= 64 * 1024 * 1024
                native[str(entry)] = hashlib.sha256(payload).hexdigest()
        output = {
            "node": request.node.nodeid,
            "source": os.environ["SNAPSHOT_DIAGNOSTIC_SOURCE"],
            "python": sys.version,
            "cc_transcript_version": distribution.version,
            "native_files_sha256": native,
            "records": records,
            "logical_rounds": rounds,
            "dropped_records": dropped,
            "validator_cache_before": validator_cache_before,
            "validator_cache_after": validation.validator.cache_info()._asdict(),
            "owner_service_admission_queue": "NOT_EXERCISED: direct FixtureOwner invokes Owner.call",
            "native_interval": (
                "Includes native validation/core/encode and GIL return; internal wait not separately observable"
            ),
            "production_validation": (
                "Same checked functions in production OwnerService; no production transport or queue exercised here"
            ),
            "whole_logical_interval": (
                "FixtureOwner.load entry through RemoteSession.release completion;"
                " narrow caller overhead outside method boundaries is excluded"
            ),
            "limits": (
                "Unchanged strict fixture; nested durations overlap,"
                " metadata/logging are included in completion_total, instrumentation adds overhead"
            ),
        }
        Path(os.environ["SNAPSHOT_DIAGNOSTIC_OUTPUT"]).write_text(json.dumps(output, indent=2) + "\n")
        assert records and len(rounds) >= 30 and dropped == 0
        assert distribution.version == "14.45.35"
        assert (
            native.get("cc_transcript/_native.abi3.so")
            == "8b326e0d8e3b79a4d079a65b5e61f984c83a78dd2384c563aff174b20d68c669"
        )
