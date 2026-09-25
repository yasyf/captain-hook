from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cc_transcript import _native
from cc_transcript.context import ContextWindow, capture_windows
from cc_transcript.ids import EventRef, EventUuid, SessionId
from cc_transcript.mining.candidates import dedup_key

from captain_hook.review.judge import CONTEXT_BUDGET, TRIGGER_BUDGET, prompt_builder
from captain_hook.review.scan import ScanReport, ingest, prepare_source, scan
from captain_hook.snapshots.client import CURRENT_CLIENT, EvidenceIncomplete, SnapshotClient
from captain_hook.snapshots.review import (
    REVIEW_POLICY,
    RenderedEvidence,
    ReviewPolicy,
    decode_candidate,
    render_review_windows,
)
from tests.review_helpers import CORRECTION, REPO, correction_entries, parse

HANDLE = {"owner_epoch": "epoch", "snapshot_id": "source", "generation": "1", "lease_id": "lease"}


def window(session: str = "session") -> ContextWindow:
    return ContextWindow(
        anchor=EventRef(SessionId(session), EventUuid("anchor")),
        before=(),
        trigger=None,
        after=(),
        fidelity="full",
        preview_chars=200,
    )


def description(handle):
    return {
        "handle": handle,
        "lease_expires_unix_ms": 9_000_000_000_000_000,
        "canonical_path": "/fixtures/session.jsonl",
        "source_id": "source",
        "device": "1",
        "inode": "1",
        "mtime_ns": "1",
        "ctime_ns": "1",
        "provider": "claude",
        "parser_version": "1",
        "source_bytes": 1,
        "committed_bytes": 1,
        "event_count": 1,
        "turn_count": 1,
        "classifier": {"id": "native", "version": "1"},
        "provisional_tail": False,
    }


def response(request, data, *, status="ok", cursor=None):
    return {
        "schema": "captain.transcript/1",
        "response": {
            "schema": "cc-transcript.snapshot/1",
            "id": request["request"]["id"],
            "status": status,
            "complete": status == "ok",
            "cursor": cursor,
            "data": data,
            "reason": None if status == "ok" else "fixture",
            "usage": dict.fromkeys(
                (
                    "source_opens",
                    "source_bytes_read",
                    "bytes_decoded",
                    "events_parsed",
                    "cold_parses",
                    "append_parses",
                    "activity_lifts",
                    "cache_hits",
                    "inflight_joins",
                    "generations_published",
                    "generations_invalidated",
                    "requests_cancelled",
                    "requests_failed",
                    "output_bytes",
                    "transport_bytes",
                    "nonincremental_lowering_calls",
                    "nonincremental_lowering_source_bytes",
                    "discovery_entries_examined",
                ),
                0,
            ),
        },
    }


class Wire:
    def __init__(self, *, bad_session=None, missing_session=None):
        self.calls = []
        self.bad_session = bad_session
        self.missing_session = missing_session
        self.released = []

    def __call__(self, envelope):
        request = envelope["request"]
        self.calls.append(request)
        match request["operation"]:
            case "resolve":
                session = request["session_ids"][0]
                if session == self.bad_session:
                    return response(envelope, None, status="parse_error")
                details = description(HANDLE | {"lease_id": session})
                return response(
                    envelope,
                    {
                        "kind": "resolved",
                        "sessions": [
                            {
                                "session_id": session,
                                "status": "missing" if session == self.missing_session else "ok",
                                "description": None if session == self.missing_session else details,
                            }
                        ],
                    },
                )
            case "hydrate":
                return response(
                    envelope,
                    {
                        "kind": "hydrated",
                        "windows": [
                            {"input_index": i, "availability": "full", "rendered": "original " + CORRECTION}
                            for i, _ in enumerate(request["windows_json"])
                        ],
                    },
                )
            case "release":
                self.released.append(request["token"])
                return response(envelope, {"kind": "released", "released": True})
        raise AssertionError(request)


def render_budget():
    return {"before": asdict(CONTEXT_BUDGET), "trigger": asdict(TRIGGER_BUDGET), "after": asdict(CONTEXT_BUDGET)}


def test_render_groups_sessions_and_releases_before_return():
    wire = Wire()
    token = CURRENT_CLIENT.set(SnapshotClient(wire))
    try:
        result = render_review_windows(
            [window("a"), window("b"), window("a")], roots=["/fixtures"], render=render_budget()
        )
    finally:
        CURRENT_CLIENT.reset(token)
    assert wire.released == ["a", "b"]
    assert [call["operation"] for call in wire.calls] == ["resolve", "hydrate", "release"] * 2
    assert all(isinstance(item, RenderedEvidence) for item in result)
    assert result[0].reference == "transcript:epoch:source:1"
    assert result[0].text == result[2].text


def test_failed_session_is_not_downgraded_to_summary_or_poison_healthy_session():
    wire = Wire(bad_session="bad", missing_session="gone")
    token = CURRENT_CLIENT.set(SnapshotClient(wire))
    try:
        result = render_review_windows(
            [window("bad"), window("good"), window("gone")], roots=["/fixtures"], render=render_budget()
        )
    finally:
        CURRENT_CLIENT.reset(token)
    assert isinstance(result[0], EvidenceIncomplete)
    assert isinstance(result[1], RenderedEvidence)
    assert result[2] is None
    assert wire.released == ["good"]


def test_partial_resolve_releases_already_issued_lease():
    wire = Wire()

    def exchange(envelope):
        if envelope["request"]["operation"] == "resolve":
            return response(
                envelope,
                {
                    "kind": "resolved",
                    "sessions": [
                        {
                            "session_id": "session",
                            "status": "ok",
                            "description": description(HANDLE),
                        }
                    ],
                },
                status="incomplete",
                cursor="next",
            )
        if envelope["request"]["operation"] == "resume":
            return response(envelope, None, status="incomplete")
        return wire(envelope)

    token = CURRENT_CLIENT.set(SnapshotClient(exchange))
    try:
        [result] = render_review_windows([window()], roots=["/fixtures"], render=render_budget())
    finally:
        CURRENT_CLIENT.reset(token)
    assert isinstance(result, EvidenceIncomplete)
    assert wire.released == ["lease"]


async def test_prompt_is_frozen_after_first_preparation(monkeypatch):
    from cc_transcript.judge import similar

    suggestions = AsyncMock(return_value=[])
    monkeypatch.setattr(similar, "suggest_canonical_keys", suggestions)
    store = SimpleNamespace(db=object(), versions=SimpleNamespace(create=1))
    row = {
        "dedup_key": "key",
        "source_kind": "transcript_message",
        "text": CORRECTION,
        "context_json": window().to_json(),
    }
    contexts = {"key": "original context"}
    fidelities = {}
    build = prompt_builder(fidelities, store, contexts, suggesting=True)
    first = await build(row)
    contexts["key"] = "changed context"
    row["text"] = "changed feedback"
    assert await build(row) == first
    assert suggestions.await_count == 1
    assert fidelities == {"key": "full"}


async def test_owner_mining_captures_same_borrowed_snapshot(monkeypatch, tmp_path):
    entries = correction_entries(session="sess-1")
    raw = "".join(json.dumps(entry) + "\n" for entry in entries).encode()
    events = parse(entries)
    captures = []

    def capture(anchors):
        captures.append(anchors)
        return capture_windows(raw, anchors)

    snapshot = SimpleNamespace(
        events=events,
        source_facts=lambda **kwargs: {"cwds": [], "first_user_contains": False},
        prose_rows=lambda: iter(()),
        capture=capture,
        description={"canonical_path": str(tmp_path / "source.jsonl"), "mtime_ns": "123"},
        checkpoint=lambda: None,
        consume=lambda **kwargs: None,
        mine_json=lambda spec, formats: _native.mine_events(events, spec, [entry[:3] for entry in formats]),
    )
    result = await ReviewPolicy().prepare_review(
        snapshot,
        {
            "policy": REVIEW_POLICY,
            "repo_key": REPO,
            "min_confidence": 0.5,
            "min_confidence_fix": 0.5,
            "decision_log_path": str(tmp_path / "decisions.db"),
            "claude_config_dir": str(tmp_path / "config"),
            "limits": {"max_output_bytes": 1048576, "max_items": 256},
        },
    )
    assert result["disposition"] == "eligible"
    assert len(captures) == 1
    candidates = [decode_candidate(raw)[0] for raw in result["candidates_json"]]
    assert any(candidate.text == CORRECTION for candidate in candidates)
    assert all(candidate.ref in captures[0] for candidate in candidates)


async def test_below_confidence_signal_never_materializes_an_event(monkeypatch, tmp_path):
    class NoEvents:
        def __getitem__(self, index):
            raise AssertionError("rejected evidence must not materialize events")

    signal = SimpleNamespace(kind="correction", signal=SimpleNamespace(confidence=0.1))
    monkeypatch.setattr("cc_transcript.mining.mine_snapshot", lambda *_: iter([signal]))
    snapshot = SimpleNamespace(
        events=NoEvents(),
        source_facts=lambda **kwargs: {"cwds": [], "first_user_contains": False},
        prose_rows=lambda: iter(()),
        description={"canonical_path": str(tmp_path / "source.jsonl"), "mtime_ns": "123"},
        checkpoint=lambda: None,
        consume=lambda **kwargs: None,
    )
    result = await ReviewPolicy().prepare_review(
        snapshot,
        {
            "policy": REVIEW_POLICY,
            "repo_key": REPO,
            "min_confidence": 0.5,
            "min_confidence_fix": 0.5,
            "limits": {"max_output_bytes": 1048576, "max_items": 256},
        },
    )
    assert result["candidates_json"] == []


def test_source_preparation_releases_lease_on_incomplete(settings):
    released = []
    session = SimpleNamespace(view=lambda: {}, release=lambda: released.append(True))

    def pages(*args, **kwargs):
        yield {"kind": "review", "candidates_json": [], "cwd": None}
        raise EvidenceIncomplete("output_limit", "bounded")

    client = SimpleNamespace(acquire=lambda path: session, pages=pages)
    with pytest.raises(EvidenceIncomplete):
        prepare_source(client, Path("/unused"), settings)
    assert released == [True]


async def test_incomplete_source_cannot_advance_scan_checkpoint(store, settings, monkeypatch):
    from captain_hook.review import scan as scan_module

    class Client:
        def pages(self, operation, **kwargs):
            assert operation == "discover"
            yield {
                "entries": [{"path": "/fixture/session.jsonl", "state": "present", "revision": "r1", "mtime_ns": "1"}],
                "checkpoint": "next",
            }

    token = CURRENT_CLIENT.set(Client())
    monkeypatch.setattr(
        scan_module,
        "prepare_source",
        lambda *args: (_ for _ in ()).throw(EvidenceIncomplete("output_limit", "bounded")),
    )
    try:
        with pytest.raises(EvidenceIncomplete):
            await scan(store, settings=settings, transcripts=[Path("/fixture")])
    finally:
        CURRENT_CLIENT.reset(token)
    assert await store.file_mtimes() == {}
    assert await store.meta(f"snapshot_discovery:{dedup_key('/fixture')}") is None
    assert await store.meta(f"snapshot_revision:{dedup_key('/fixture/session.jsonl')}") is None


async def test_ingest_records_empty_source_atomically(store):
    prepared = {
        "canonical_path": "/fixture/source.jsonl",
        "mtime_ns": "1230000000",
        "repo_key": REPO,
        "candidates_json": [],
    }
    assert await ingest(store, prepared, corrections=[]) == ScanReport(1, 0)
    assert await store.file_mtimes() == {"/fixture/source.jsonl": 1.23}


async def test_ingest_failure_rolls_back_source_and_feedback(store, monkeypatch):
    from captain_hook.review.scan import detect, to_candidate
    from captain_hook.snapshots.review import encode_candidate

    signal = next(detect(parse(correction_entries())))
    candidate = to_candidate(window(), signal)
    prepared = {
        "canonical_path": "/fixture/source.jsonl",
        "mtime_ns": "1230000000",
        "repo_key": REPO,
        "candidates_json": [encode_candidate(signal, candidate)],
    }
    monkeypatch.setattr(store, "record_observation", AsyncMock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        await ingest(store, prepared, corrections=[])
    assert await store.file_mtimes() == {}
    assert await store.db.sql("SELECT dedup_key FROM feedback_events") == []


async def test_correction_drafts_keep_full_hunks_without_post_model_snapshot_access(monkeypatch):
    from cc_transcript.activity import SessionActivity
    from cc_transcript.extract.correct import CorrectionPick

    from captain_hook.snapshots.review import prepare_corrections, record_correction_drafts
    from tests.review_helpers import assistant_tool_use, user_text

    old = "x" * 3000
    new = "y" * 3000
    entries = [
        assistant_tool_use("edit", "Edit", {"file_path": "x.py", "old_string": old, "new_string": new}),
        user_text(CORRECTION, uuid="feedback"),
    ]
    activity = SessionActivity.from_events(SessionId("sess-1"), parse(entries))
    borrowed = [True]

    def get_activity(classifier, **kwargs):
        assert borrowed[0]
        return activity

    snapshot = SimpleNamespace(activity=get_activity, checkpoint=lambda: None, consume=lambda **kwargs: None)
    prepared = await prepare_corrections(
        snapshot,
        {
            "policy": REVIEW_POLICY,
            "view": {"classifier": {}},
            "anchors": [asdict(EventRef(SessionId("sess-1"), EventUuid("feedback")))],
            "feedback": [CORRECTION],
            "repo": None,
            "limits": {"max_output_bytes": 1048576, "max_read_bytes": 1048576},
        },
    )
    borrowed[0] = False
    recorded = []

    class Log:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def for_anchor(self, *args):
            return []

        async def append(self, row):
            recorded.append(row)

    async def choose(prompt, schema, **kwargs):
        assert not borrowed[0]
        assert CORRECTION in prompt
        return CorrectionPick(candidate=1, note="fixture")

    monkeypatch.setattr("cc_transcript.corrections.CorrectionLog.open", AsyncMock(return_value=Log()))
    monkeypatch.setattr("cc_transcript.extract.correct.usable_backend", lambda: object())
    monkeypatch.setattr("spawnllm.extract", choose)
    await record_correction_drafts(prepared["corrections"])
    assert len(recorded) == 1
    assert recorded[0].incorrect_old == old
    assert recorded[0].incorrect_new == new
    assert recorded[0].incorrect_digest


async def test_correction_hunks_rejected_before_copying(monkeypatch):
    from captain_hook.snapshots.review import prepare_corrections

    pair = SimpleNamespace(incorrect=SimpleNamespace(hunks=[SimpleNamespace(old="x" * 200, new="")]), correction=None)
    activity = SimpleNamespace(turn_of=lambda anchor: SimpleNamespace(index=1))
    snapshot = SimpleNamespace(
        activity=lambda classifier, **kwargs: activity, checkpoint=lambda: None, consume=lambda **kwargs: None
    )
    monkeypatch.setattr("cc_transcript.evidence.harvest_pairs", lambda *args, **kwargs: [pair])
    monkeypatch.setattr(
        "cc_transcript.evidence.lower_pair", lambda *args, **kwargs: pytest.fail("must reject before copying")
    )
    with pytest.raises(EvidenceIncomplete, match="cumulative output budget"):
        await prepare_corrections(
            snapshot,
            {
                "policy": REVIEW_POLICY,
                "view": {"classifier": {}},
                "anchors": [asdict(window().anchor)],
                "feedback": ["feedback"],
                "repo": None,
                "limits": {"max_output_bytes": 100, "max_read_bytes": 100},
            },
        )


async def test_policy_reuses_decision_handles_and_closes_replaced_files(tmp_path, monkeypatch):
    opened = []

    async def open_log(path):
        path.touch()
        log = SimpleNamespace(close=AsyncMock())
        opened.append(log)
        return log

    monkeypatch.setattr("captain_hook.decisions.open_decision_log", open_log)
    policy = ReviewPolicy()
    path = tmp_path / "decisions.db"
    first = await policy.decision_log(path)
    assert await policy.decision_log(path) is first
    path.rename(tmp_path / "old.db")
    path.touch()
    assert await policy.decision_log(path) is not first
    assert len(opened) == 2
    first.close.assert_awaited_once()
    await policy.close()
    opened[1].close.assert_awaited_once()


async def test_policy_bounds_decision_handles(tmp_path, monkeypatch):
    opened = []

    async def open_log(path):
        path.touch()
        log = SimpleNamespace(close=AsyncMock())
        opened.append(log)
        return log

    monkeypatch.setattr("captain_hook.decisions.open_decision_log", open_log)
    policy = ReviewPolicy()
    for index in range(9):
        await policy.decision_log(tmp_path / f"{index}.db")
    opened[0].close.assert_awaited_once()
    assert len(policy._decisions) == 8
    await policy.close()
    assert all(log.close.await_count == 1 for log in opened)


def test_git_budget_prevents_process_start(monkeypatch):
    from captain_hook.snapshots.review import BoundedGit

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("exhausted budget launched Git"))
    snapshot = SimpleNamespace(checkpoint=lambda: None, consume=lambda **kwargs: None)
    with pytest.raises(EvidenceIncomplete, match="work budget"):
        BoundedGit(snapshot, max_bytes=0)(Path("/fixture"), "show", "HEAD")


def test_git_output_is_bounded_before_accumulation_and_own_child_is_reaped(monkeypatch):
    from captain_hook.snapshots.review import BoundedGit

    calls = []
    process = SimpleNamespace(
        stdout=SimpleNamespace(close=lambda: None),
        stderr=SimpleNamespace(close=lambda: None),
        poll=lambda: None,
        kill=lambda: calls.append("kill"),
        wait=lambda: calls.append("wait"),
    )

    def popen(command, **kwargs):
        assert "--no-ext-diff" in command
        assert "--no-textconv" in command
        assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        return process

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def register(self, *args):
            pass

        def get_map(self):
            return True

        def select(self, **kwargs):
            return [(SimpleNamespace(fd=1, data=True), None)]

    def read(fd, amount):
        assert amount == 11
        return b"x" * amount

    monkeypatch.setattr("subprocess.Popen", popen)
    monkeypatch.setattr("selectors.DefaultSelector", Selector)
    monkeypatch.setattr("os.read", read)
    snapshot = SimpleNamespace(checkpoint=lambda: None, consume=lambda **kwargs: None)
    with pytest.raises(EvidenceIncomplete, match="Git output"):
        BoundedGit(snapshot, max_bytes=10)(Path("/fixture"), "show", "HEAD")
    assert calls == ["kill", "wait"]
