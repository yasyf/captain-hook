from __future__ import annotations

import itertools
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from captain_hook.snapshots.client import (
    CORE_SCHEMA,
    CURRENT_CLIENT,
    SnapshotClient,
    SnapshotProtocolError,
    encode_frame,
    read_frame,
)
from captain_hook.snapshots.validation import checked, validator

if TYPE_CHECKING:
    from cc_transcript.snapshots import CallContext

    from captain_hook.snapshots.client import RemoteSession

BUILD = "fixture"


def empty_usage() -> dict[str, int]:
    return dict.fromkeys(validator("response").schema["$defs"]["Usage"]["properties"], 0)


def failure(request_id: str, status: str, reason: str, usage: dict[str, int] | None = None) -> dict[str, Any]:
    counts = empty_usage() | (usage or {})
    counter = "requests_cancelled" if status == "cancelled" else "requests_failed"
    counts[counter] = max(counts[counter], 1)
    return {
        "schema": CORE_SCHEMA,
        "id": request_id,
        "status": status,
        "complete": False,
        "data": None,
        "cursor": None,
        "reason": reason[:4096],
        "usage": counts,
    }


class FixtureOwner:
    def __init__(self) -> None:
        config = {name: field["default"] for name, field in validator("config").schema["properties"].items()}
        self.process = subprocess.Popen(
            [str(Path(sys.executable).with_name("cc-transcript-owner")), "--build", BUILD],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.process.stdin.write(
            encode_frame({"protocol": 1, "op": "hello", "build": BUILD, "snapshot_config": config})
        )
        self.process.stdin.flush()
        if read_frame(self.process.stdout) != {"protocol": 1, "op": "hello", "build": BUILD}:
            raise SnapshotProtocolError("fixture snapshot owner rejected its handshake")
        self.context: CallContext = {
            "claimant": "fixture",
            "admission": "hook",
            "authority": {"kind": "user", "effective_uid": str(os.getuid())},
            "registry_generation": "fixture",
        }
        self.client = SnapshotClient(self.exchange)

    def call(self, wrapper: dict[str, object], context: CallContext) -> dict[str, Any]:
        request = checked("host-request", wrapper)
        with self.lock:
            frame_id = next(self.ids)
            self.process.stdin.write(
                encode_frame(
                    {
                        "protocol": 1,
                        "op": "snapshot_request",
                        "id": frame_id,
                        "snapshot": request,
                        "snapshot_context": context,
                    }
                )
            )
            self.process.stdin.flush()
            reply = read_frame(self.process.stdout)
        if reply["op"] != "snapshot_result" or reply["id"] != frame_id:
            raise SnapshotProtocolError(str(reply.get("error", "fixture snapshot owner answered another request")))
        return checked("host-response", reply["snapshot"])

    def exchange(self, wrapper: dict[str, object]) -> dict[str, Any]:
        return self.call(wrapper, self.context.copy())

    def client_for_context(self, **overrides: str) -> SnapshotClient:
        return SnapshotClient(lambda wrapper: self.call(wrapper, self.context | overrides))

    def load(self, path: str | Path) -> RemoteSession:
        from captain_hook.transcripts import load_transcript

        token = CURRENT_CLIENT.set(self.client)
        try:
            return load_transcript(path)
        finally:
            CURRENT_CLIENT.reset(token)

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            self.process.stdin.close()
            self.process.wait()
            self.process.stdout.close()


def fixture_transcript(path: str | Path) -> RemoteSession:
    """Read an explicit fixture through a private native owner without launching a helper."""
    fixture = FixtureOwner()
    return fixture.load(path)
