from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from captain_hook.snapshots.client import CURRENT_CLIENT, HOST_SCHEMA, SnapshotClient
from captain_hook.snapshots.validation import checked, validator
from captain_hook.snapshots.worker import Owner

if TYPE_CHECKING:
    from cc_transcript.snapshots import CallContext

    from captain_hook.snapshots.client import RemoteSession


class FixtureOwner:
    def __init__(self) -> None:
        config = {name: field["default"] for name, field in validator("config").schema["properties"].items()}
        self.owner = Owner(config)
        self.client = SnapshotClient(self.exchange)
        self.context: CallContext = {
            "claimant": "fixture",
            "admission": "hook",
            "authority": {"kind": "user", "effective_uid": str(os.getuid())},
            "registry_generation": "fixture",
        }

    def exchange(self, wrapper: dict[str, object]) -> dict[str, Any]:
        request = checked("host-request", wrapper)
        result = self.owner.call(
            request["request"], self.context.copy(), self.owner.token_type(), request["tool_registry"]
        )
        return checked("host-response", {"schema": HOST_SCHEMA, "response": result})

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
            self.owner.close()


def fixture_transcript(path: str | Path) -> RemoteSession:
    """Read an explicit fixture through a private native owner without launching a helper."""
    fixture = FixtureOwner()
    return fixture.load(path)
