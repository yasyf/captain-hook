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
def snapshot_latency_diagnostic(request, monkeypatch):
    if request.node.nodeid != NODE:
        yield
        return

    from captain_hook.snapshots.client import RemoteSession, SnapshotClient
    from captain_hook.snapshots.worker import Owner
    from captain_hook.testing.snapshots import FixtureOwner

    records = []
    dropped = 0
    guard = threading.Lock()

    def wrap(original, layer, operation):
        def observed(instance, *args, **kwargs):
            nonlocal dropped
            started = time.perf_counter_ns()
            response = None
            error = None
            try:
                response = original(instance, *args, **kwargs)
                return response
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                finished = time.perf_counter_ns()
                row = {
                    "layer": layer,
                    "operation": operation(args),
                    "thread": threading.current_thread().name,
                    "started_ns": started,
                    "finished_ns": finished,
                    "elapsed_ns": finished - started,
                    "error_type": error,
                }
                if layer == "owner":
                    row["work_class"] = "background" if args[1].get("work_class") == "background" else "foreground"
                if layer in {"owner", "client"} and isinstance(response, dict):
                    row["status"] = response.get("status")
                    row["usage"] = {
                        key: value for key, value in response.get("usage", {}).items() if type(value) is int
                    }
                with guard:
                    if len(records) < 4096:
                        records.append(row)
                    else:
                        dropped += 1

        return observed

    bindings = [
        (FixtureOwner, "load", "acquisition", lambda args: "load"),
        (RemoteSession, "has_edit_to", "query", lambda args: "has_edit_to"),
        (RemoteSession, "release", "release", lambda args: "release"),
        (SnapshotClient, "call", "client", lambda args: args[0]),
        (Owner, "call", "owner", lambda args: args[0]["operation"]),
    ]
    for cls, name, layer, operation in bindings:
        monkeypatch.setattr(cls, name, wrap(getattr(cls, name), layer, operation))

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
            "dropped_records": dropped,
            "owner_service_admission_queue": "not exercised: FixtureOwner directly invokes Owner.call",
            "native_internal_wait": "not separately observable from this Python seam",
            "limits": "unchanged strict fixture; nested durations overlap and instrumentation adds overhead",
        }
        assert records and native
        Path(os.environ["SNAPSHOT_DIAGNOSTIC_OUTPUT"]).write_text(json.dumps(output, indent=2) + "\n")
