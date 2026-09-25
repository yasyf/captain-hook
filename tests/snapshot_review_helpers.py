from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cc_transcript import parse_events_from_bytes
from cc_transcript.activity import SessionActivity
from cc_transcript.context import ContextWindow, HydratedWindow, capture_windows, hydrate_from_activity
from cc_transcript.filterspec import event_meta
from cc_transcript.render import Budget

from captain_hook.snapshots.client import CURRENT_CLIENT, DEFAULT_LIMITS, EvidenceIncomplete
from captain_hook.snapshots.review import ReviewPolicy


class FixtureSnapshot:
    def __init__(self, path: Path, handle):
        self.raw = path.read_bytes()
        self.events = parse_events_from_bytes(self.raw)
        self.description = {
            "canonical_path": str(path),
            "mtime_ns": str(path.stat().st_mtime_ns),
            "handle": handle,
            "lease_expires_unix_ms": 9_000_000_000_000_000,
        }
        self.activities = {}

    def activity(self, classifier, **kwargs):
        session_id = next(meta.session_id for event in self.events if (meta := event_meta(event)) is not None)
        if session_id not in self.activities:
            self.activities[session_id] = SessionActivity.from_events(session_id, self.events)
        return self.activities[session_id]

    def mine_json(self, spec_json, formats):
        from cc_transcript import _native

        return _native.mine_events(self.events, spec_json, [entry[:3] for entry in formats])

    def capture(self, anchors):
        return capture_windows(self.raw, anchors)

    def checkpoint(self):
        pass

    def consume(self, **kwargs):
        pass


class FixtureOwner:
    def __init__(self):
        self._leases = set()
        self.policy = ReviewPolicy()
        self.snapshots = {}
        self.calls = []
        self.released = []

    def call(self, operation, **arguments):
        assert operation == "release"
        self.released.append(arguments["token"])
        return {"status": "ok"}

    def acquire(self, path):
        path = Path(path)
        token = str(len(self.snapshots))
        handle = {"owner_epoch": "fixture", "snapshot_id": str(path), "generation": token, "lease_id": token}
        try:
            self.snapshots[token] = FixtureSnapshot(path, handle)
        except (ValueError, KeyError) as exc:
            raise EvidenceIncomplete("parse_error", str(exc)) from exc
        return SimpleNamespace(
            view=lambda: {"handle": handle, "classifier": {"id": "native", "version": "1"}},
            release=lambda: self.released.append(token),
        )

    def pages(self, operation, **arguments):
        self.calls.append((operation, arguments))
        if operation == "discover":
            entries = []
            for root in arguments["roots"]:
                root = Path(root)
                for path in sorted(root.rglob("*.jsonl")) if root.is_dir() else [root]:
                    if not path.exists():
                        continue
                    stat = path.stat()
                    entries.append(
                        {
                            "path": str(path),
                            "mtime_ns": str(stat.st_mtime_ns),
                            "state": "present",
                            "revision": f"{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}",
                        }
                    )
            yield {"kind": "discovered", "entries": entries, "checkpoint": "fixture-checkpoint"}
        elif operation in {"prepare_review", "prepare_corrections"}:
            snapshot = self.snapshots[arguments["view"]["handle"]["lease_id"]]
            handler = self.policy.prepare_review if operation == "prepare_review" else self.policy.prepare_corrections
            yield asyncio.run(handler(snapshot, arguments | {"limits": DEFAULT_LIMITS}))
        elif operation == "resolve":
            result = []
            for session in arguments["session_ids"]:
                found = None
                for root in arguments["roots"]:
                    for path in sorted(Path(root).rglob("*.jsonl")):
                        try:
                            events = parse_events_from_bytes(path.read_bytes())
                        except (ValueError, KeyError):
                            continue
                        if any(
                            (meta := event_meta(event)) is not None and meta.session_id == session for event in events
                        ):
                            remote = self.acquire(path)
                            found = self.snapshots[remote.view()["handle"]["lease_id"]].description
                            break
                    if found is not None:
                        break
                result.append({"session_id": session, "status": "ok" if found else "missing", "description": found})
            yield {"kind": "resolved", "sessions": result}
        elif operation == "hydrate":
            bundle = {item["session_id"]: self.snapshots[item["handle"]["lease_id"]] for item in arguments["handles"]}
            results = []
            for index, raw in enumerate(arguments["windows_json"]):
                window = ContextWindow.from_json(raw)
                hydrated = hydrate_from_activity(window, bundle[window.anchor.session_id].activity({}))
                text = None
                if hydrated is not None:
                    split = len(window.before)
                    end = split + (window.trigger is not None)
                    text = "\n".join(
                        f"=== {label} ===\n"
                        + (HydratedWindow(window, turns).render(budget=Budget(**budget)) or "(none)")
                        for label, turns, budget in (
                            ("conversation before", hydrated.turns[:split], arguments["render"]["before"]),
                            (
                                "the turn the feedback arrived in",
                                hydrated.turns[split:end],
                                arguments["render"]["trigger"],
                            ),
                            ("conversation after", hydrated.turns[end:], arguments["render"]["after"]),
                        )
                    )
                results.append(
                    {
                        "input_index": index,
                        "availability": "full" if text is not None else "missing_ref",
                        "rendered": text,
                    }
                )
            yield {"kind": "hydrated", "windows": results}
        else:
            raise AssertionError(operation)


@contextmanager
def owner_fixture(monkeypatch):
    owner = FixtureOwner()
    token = CURRENT_CLIENT.set(owner)

    def forbidden_bridge(*args, **kwargs):
        raise AssertionError("review tests may not launch a live snapshot helper")

    monkeypatch.setattr("captain_hook.snapshots.client.Bridge.__init__", forbidden_bridge)
    monkeypatch.setattr("captain_hook.snapshots.review.review_client", lambda: nullcontext(owner))
    monkeypatch.setattr(
        "captain_hook.snapshots.review.review_roots",
        lambda paths: sorted({str(Path(path).parent) for path in paths if Path(path).is_absolute()}),
    )
    monkeypatch.setattr("captain_hook.snapshots.review.record_correction_drafts", AsyncMock())
    try:
        yield owner
    finally:
        CURRENT_CLIENT.reset(token)
        asyncio.run(owner.policy.close())
