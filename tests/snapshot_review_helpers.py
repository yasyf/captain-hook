from __future__ import annotations

from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cc_transcript import parse_events_from_bytes
from cc_transcript.activity import SessionActivity
from cc_transcript.context import ContextWindow, HydratedWindow, capture_windows, hydrate_from_activity
from cc_transcript.filterspec import event_meta
from cc_transcript.ids import EventRef
from cc_transcript.render import Budget

from captain_hook.snapshots.client import CURRENT_CLIENT, NATIVE_CLASSIFIER, EvidenceIncomplete, ReviewEvents


class FixtureSnapshot:
    def __init__(self, path: Path, handle):
        self.raw = path.read_bytes()
        self.events = parse_events_from_bytes(self.raw)
        self.description = {
            "canonical_path": str(path),
            "mtime_ns": str(path.stat().st_mtime_ns),
            "source_revision": f"fixture:{path.stat().st_ino}:{path.stat().st_mtime_ns}:{len(self.raw)}",
            "handle": handle,
            "lease_expires_unix_ms": 9_000_000_000_000_000,
        }
        self.activities = {}

    def activity(self, classifier, **kwargs):
        session_id = next(meta.session_id for event in self.events if (meta := event_meta(event)) is not None)
        if session_id not in self.activities:
            self.activities[session_id] = SessionActivity.from_events(session_id, self.events)
        return self.activities[session_id]

    def review_events(self, watermark, context_events):
        fresh = int(watermark) if watermark is not None and int(watermark) <= len(self.events) else 0
        start = max(0, fresh - context_events)
        return ReviewEvents(
            self.description["source_revision"],
            self.description["canonical_path"],
            int(self.description["mtime_ns"]),
            tuple(self.events[start:]),
            tuple(range(start, len(self.events))),
            fresh,
            str(len(self.events)),
            True,
        )


class FixtureOwner:
    def __init__(self):
        self._defer_cleanup = False
        self._leases = set()
        self.foreground_deadline_unix_ms = None
        self.snapshots = {}
        self.calls = []
        self.released = []

    def call(self, operation, **arguments):
        assert operation == "release"
        self.released.append(arguments["token"])
        return {"status": "ok"}

    def acquire(self, path, *, classifier=NATIVE_CLASSIFIER):
        path = Path(path)
        token = str(len(self.snapshots))
        handle = {"owner_epoch": "fixture", "snapshot_id": str(path), "generation": token, "lease_id": token}
        try:
            self.snapshots[token] = FixtureSnapshot(path, handle)
        except (ValueError, KeyError) as exc:
            raise EvidenceIncomplete("parse_error", str(exc)) from exc
        lease = SimpleNamespace(
            require=lambda: handle,
            release=lambda: self.released.append(token),
            description=self.snapshots[token].description,
        )
        return SimpleNamespace(
            view=lambda: {"handle": handle, "classifier": dict(classifier)},
            lease=lease,
            path=path,
            release=lease.release,
        )

    def review_events(self, path, *, watermark, context_events, max_pages):
        self.calls.append(
            ("review_events", {"path": str(path), "watermark": watermark, "context_events": context_events})
        )
        try:
            snapshot = FixtureSnapshot(Path(path), None)
        except (ValueError, KeyError) as exc:
            raise EvidenceIncomplete("parse_error", str(exc)) from exc
        return snapshot.review_events(watermark, context_events)

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
        elif operation == "capture":
            snapshot = self.snapshots[arguments["view"]["handle"]["lease_id"]]
            windows = capture_windows(snapshot.raw, [EventRef(**anchor) for anchor in arguments["anchors"]])
            yield {"kind": "captured", "windows_json": [window.to_json() for window in windows]}
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
    monkeypatch.setattr("captain_hook.review.scan.CorrectionLedger.recorded", AsyncMock(return_value=set()))
    try:
        yield owner
    finally:
        CURRENT_CLIENT.reset(token)
