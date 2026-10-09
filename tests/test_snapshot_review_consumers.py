from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cc_transcript import _native
from cc_transcript.context import ContextWindow, capture_windows
from cc_transcript.ids import EventRef, EventUuid, SessionId
from cc_transcript.mining.candidates import dedup_key
from spawnllm import Decision, LabelAnswer, Refused

from captain_hook.review.judge import CONTEXT_BUDGET, TRIGGER_BUDGET, prompt_builder
from captain_hook.review.scan import CorrectionLedger, ScanReport, ingest, prepare_source, scan
from captain_hook.snapshots.client import CURRENT_CLIENT, EvidenceIncomplete, SnapshotClient
from captain_hook.snapshots.review import (
    REVIEW_POLICY,
    RenderedEvidence,
    ReviewPolicy,
    decode_candidate,
    render_review_windows,
)
from tests.review_helpers import (
    CORRECTION,
    REPO,
    assistant_text,
    assistant_tool_use,
    correction_entries,
    native_review_owner,
    parse,
    tool_result,
    user_text,
    write_transcript,
)

HANDLE = {"owner_epoch": "epoch", "snapshot_id": "source", "generation": "1", "lease_id": "lease"}
TOOL_RESULT_BYTES = 64 * 1024
HOOK_COMPLAINT_TEXT = "**Note**: The task tracker reminder re-fired on a sequence I already completed - ignoring it."


def nothing_recorded(anchors) -> set[EventRef]:
    return set()


async def recorded_in_ledger(anchors) -> set[EventRef]:
    async with CorrectionLedger() as ledger:
        return await ledger.recorded(anchors)


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
        "window_start": 0,
        "window_started_unix_ms": None,
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
        prepare_source(client, Path("/unused"), settings, recorded=nothing_recorded)
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
        lambda *args, **kwargs: (_ for _ in ()).throw(EvidenceIncomplete("deadline", "bounded")),
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
    async with CorrectionLedger() as ledger:
        assert await ingest(store, prepared, corrections=[], ledger=ledger) == ScanReport(1, 0)
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
        await ingest(store, prepared, corrections=[], ledger=CorrectionLedger())
    assert await store.file_mtimes() == {}
    assert await store.db.sql("SELECT dedup_key FROM feedback_events") == []


@pytest.mark.parametrize(
    ("jev", "recorded_rows", "llm_picks"),
    [("1", 1, False), ("none", 0, False), (TimeoutError("no jev"), 1, True), (Refused(), 1, True)],
)
async def test_correction_drafts_keep_full_hunks_without_post_model_snapshot_access(
    monkeypatch, jev, recorded_rows, llm_picks
):
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

    chosen_by_llm = []

    async def choose(prompt, schema, **kwargs):
        assert not borrowed[0]
        assert CORRECTION in prompt
        chosen_by_llm.append(prompt)
        return CorrectionPick(candidate=1, note="fixture")

    async def decide(state, questions, **kwargs):
        assert not borrowed[0]
        assert CORRECTION in state
        assert list(questions["candidate"].options) == ["none", "1"]
        if isinstance(jev, BaseException):
            raise jev
        answer = jev if isinstance(jev, Refused) else LabelAnswer(jev, {jev: 0.9}, 0.8)
        return Decision({"candidate": answer}, "jev-1.13.0", 120, 95.0)

    monkeypatch.setattr("cc_transcript.extract.correct.usable_backend", lambda: object())
    monkeypatch.setattr("spawnllm.extract", choose)
    monkeypatch.setattr("spawnllm.decide", decide)
    await record_correction_drafts(
        prepared["corrections"], ledger=SimpleNamespace(handle=AsyncMock(return_value=Log()))
    )
    assert bool(chosen_by_llm) is llm_picks
    assert len(recorded) == recorded_rows
    if recorded_rows:
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


def bulky_turn(prompt: str, calls: int) -> list[dict]:
    return [
        user_text(prompt),
        *(
            entry
            for call in range(calls)
            for entry in (
                assistant_tool_use(f"{prompt}-{call}", "Bash", {"command": f"cat log-{call}"}),
                tool_result(f"{prompt}-{call}", "r" * TOOL_RESULT_BYTES),
            )
        ),
    ]


def corrected_edit(label: str) -> list[dict]:
    return [
        assistant_tool_use(f"edit-{label}", "Edit", {"file_path": f"{label}.py", "old_string": "a", "new_string": "b"}),
        tool_result(f"edit-{label}", "ok"),
        *correction_entries(),
    ]


def review_comments(count: int) -> list[dict]:
    body = "\n".join(
        f"In src/mod{index}.py:L{index + 1}: name this helper after what it validates {'and it ' * 700}"
        for index in range(count)
    )
    return [assistant_text("rewrote the parser"), user_text(body)]


def prepared_anchors(path: Path, settings) -> tuple[Counter[str], Counter[str], list[tuple[EventRef, Any]]]:
    prepared, corrections, refused = prepare_source(
        CURRENT_CLIENT.get(), path, settings, REPO, recorded=nothing_recorded
    )
    eligible = Counter(
        candidate.ref.event_uuid
        for raw in prepared["candidates_json"]
        if (candidate := decode_candidate(raw)[0]).source_kind == "transcript_message"
    )
    return eligible, Counter(draft["anchor"]["event_uuid"] for draft in corrections), refused


@pytest.fixture
def git_calls(monkeypatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    class CountingGit:
        def __init__(self, snapshot, *, max_bytes, max_processes=84):
            self.snapshot = snapshot

        def __call__(self, repo, *arguments):
            calls.append(arguments)

    monkeypatch.setattr("captain_hook.snapshots.review.BoundedGit", CountingGit)
    return calls


@pytest.fixture
def prepared_batches(monkeypatch) -> list[list[str]]:
    from captain_hook.review import scan as scan_module

    batches: list[list[str]] = []
    split = scan_module.correction_pages

    def counted(client, session, batch, repo):
        batches.append([candidate.ref.event_uuid for candidate in batch])
        return split(client, session, batch, repo)

    monkeypatch.setattr(scan_module, "correction_pages", counted)
    return batches


def anchor_uuid(edit: list[dict]) -> str:
    return edit[-1]["uuid"]


@pytest.mark.usefixtures(native_review_owner.__name__)
async def test_rescan_prepares_only_anchors_without_a_recorded_correction(
    tmp_path, store, settings, prepared_batches, git_calls
):
    edits = [corrected_edit(f"module{index}") for index in range(10)]
    path = write_transcript(tmp_path / "s.jsonl", [entry for edit in edits[:8] for entry in edit])
    await scan(store, settings=settings, transcripts=[tmp_path], repo_key=REPO)
    assert sorted(uuid for batch in prepared_batches for uuid in batch) == sorted(map(anchor_uuid, edits[:8]))
    prepared_batches.clear()
    write_transcript(path, [entry for edit in edits for entry in edit])
    assert (await scan(store, settings=settings, transcripts=[tmp_path], repo_key=REPO)).scanned == 1
    assert prepared_batches == [[anchor_uuid(edit) for edit in edits[8:]]]


@pytest.mark.usefixtures(native_review_owner.__name__)
async def test_rescan_of_an_unchanged_transcript_prepares_nothing_and_runs_no_git(
    tmp_path, store, settings, prepared_batches, git_calls
):
    write_transcript(tmp_path / "s.jsonl", [entry for label in ("parser", "lexer") for entry in corrected_edit(label)])
    await scan(store, settings=settings, transcripts=[tmp_path], repo_key=REPO)
    assert len(prepared_batches) == 1
    prepared_batches.clear()
    git_before = len(git_calls)
    assert await scan(store, settings=settings, transcripts=[tmp_path], repo_key=REPO) == ScanReport(0, 0, 0)
    assert (prepared_batches, len(git_calls)) == ([], git_before)


@pytest.mark.usefixtures(native_review_owner.__name__)
def test_a_correction_recorded_under_another_anchor_ref_leaves_the_anchor_prepared(
    tmp_path, settings, prepared_batches
):
    import asyncio
    from dataclasses import replace

    from cc_transcript.corrections import Correction, CorrectionLog

    parser, lexer = corrected_edit("parser"), corrected_edit("lexer")
    path = write_transcript(tmp_path / "s.jsonl", [*parser, *lexer])
    _, corrections, _ = prepare_source(CURRENT_CLIENT.get(), path, settings, REPO, recorded=nothing_recorded)
    rows = {
        draft["anchor"]["event_uuid"]: Correction(**json.loads(draft["choices"][0]["correction_json"]))
        for draft in corrections
    }

    async def record() -> None:
        async with await CorrectionLog.open() as log:
            await log.append(rows[anchor_uuid(parser)])
            await log.append(replace(rows[anchor_uuid(lexer)], anchor_uuid=EventUuid("superseded-anchor")))

    asyncio.run(record())
    prepared_batches.clear()
    _, corrections, _ = prepare_source(
        CURRENT_CLIENT.get(), path, settings, REPO, recorded=lambda anchors: asyncio.run(recorded_in_ledger(anchors))
    )
    assert prepared_batches == [[anchor_uuid(lexer)]]
    assert [draft["anchor"]["event_uuid"] for draft in corrections] == [anchor_uuid(lexer)]


@pytest.fixture
def event_reads(monkeypatch) -> Counter[int]:
    from cc_transcript.snapshots import _Events

    reads: Counter[int] = Counter()
    read = _Events.__getitem__

    def counted(events, index):
        event = read(events, index)
        if isinstance(index, int):
            reads[index] += 1
        return event

    monkeypatch.setattr(_Events, "__getitem__", counted)
    return reads


def direct_event_refusal(owner, path: Path, index: int):
    from cc_transcript.snapshots import SnapshotIncomplete

    from captain_hook.snapshots.client import DEFAULT_LIMITS

    context = owner.context | {
        "registry_generation": owner.owner.store.register_tool_registry(
            owner.client.tool_registry(), context=owner.context
        )
    }
    session = owner.client.acquire(path)
    try:
        with owner.owner.store.borrow_snapshot(
            session.view()["handle"],
            context=context,
            cancellation=owner.owner.token_type(),
            limits=DEFAULT_LIMITS,
            deadline_unix_ms=int(time.time() * 1000) + 30_000,
        ) as snapshot:
            with pytest.raises(SnapshotIncomplete) as refused:
                snapshot.events[index]
    finally:
        session.release()
    return refused.value


@pytest.mark.usefixtures(native_review_owner.__name__)
def test_corrections_cover_an_anchor_after_a_turn_larger_than_one_record(tmp_path, settings):
    entries = [*bulky_turn("audit", 16), *corrected_edit("parser")]
    eligible, drafted, refused = prepared_anchors(write_transcript(tmp_path / "s.jsonl", entries), settings)
    assert len(eligible) == 1
    assert (drafted, refused) == (eligible, [])


@pytest.mark.usefixtures(native_review_owner.__name__)
def test_corrections_cover_anchors_whose_windows_together_exceed_the_scope_output(tmp_path, settings):
    entries = [
        *(entry for turn in range(6) for entry in bulky_turn(f"audit-{turn}", 5)),
        *(entry for label in ("parser", "lexer", "loader", "writer", "reader") for entry in corrected_edit(label)),
    ]
    eligible, drafted, refused = prepared_anchors(write_transcript(tmp_path / "s.jsonl", entries), settings)
    assert len(eligible) == 5
    assert (drafted, refused) == (eligible, [])


@pytest.mark.usefixtures(native_review_owner.__name__)
def test_corrections_split_a_batch_whose_combined_window_exceeds_the_scope_output(tmp_path, settings, monkeypatch):
    from captain_hook.review import scan as scan_module

    batches: list[int] = []
    split = scan_module.correction_pages

    def counted(client, session, batch, repo):
        batches.append(len(batch))
        return split(client, session, batch, repo)

    monkeypatch.setattr(scan_module, "correction_pages", counted)
    entries = [
        *(entry for turn in range(6) for entry in bulky_turn(f"early-{turn}", 12)),
        *corrected_edit("parser"),
        *(user_text(f"status {turn}") for turn in range(125)),
        *(entry for turn in range(6) for entry in bulky_turn(f"late-{turn}", 12)),
        *corrected_edit("lexer"),
    ]
    eligible, drafted, refused = prepared_anchors(write_transcript(tmp_path / "s.jsonl", entries), settings)
    assert len(eligible) == 2
    assert (drafted, refused) == (eligible, [])
    assert batches == [2, 1, 1]


@pytest.mark.usefixtures(native_review_owner.__name__)
def test_correction_anchor_whose_own_window_exceeds_the_scope_output_is_refused_by_size(tmp_path, settings):
    entries = [
        *(entry for turn in range(12) for entry in bulky_turn(f"audit-{turn}", 12)),
        *corrected_edit("parser"),
        *(user_text(f"status {turn}") for turn in range(45)),
        *corrected_edit("lexer"),
    ]
    eligible, drafted, refused = prepared_anchors(write_transcript(tmp_path / "s.jsonl", entries), settings)
    [(anchor, refusal)] = refused
    assert len(eligible) == 2
    assert drafted + Counter([anchor.event_uuid]) == eligible
    assert refusal.status == "output_limit"
    assert re.fullmatch(
        r"activity window of 59 turns \(0\.\.59\) needs \d+ bytes, over the 16777216-byte remaining output budget",
        refusal.reason,
    )


def test_correction_anchor_whose_window_holds_an_event_over_the_record_bound_is_refused(
    tmp_path, settings, native_review_owner
):
    path = write_transcript(
        tmp_path / "s.jsonl",
        [
            user_text("run the audit"),
            assistant_tool_use("big", "Bash", {"command": "cat log"}),
            tool_result("big", "r" * (1024 * 1024 + 1)),
            *corrected_edit("parser"),
        ],
    )
    eligible, drafted, refused = prepared_anchors(path, settings)
    [(anchor, refusal)] = refused
    assert (drafted, Counter([anchor.event_uuid])) == (Counter(), eligible)
    assert refusal.status == "output_limit"
    assert re.fullmatch(r"event 2 needs \d+ bytes, over the 1048576-byte record bound", refusal.reason)
    assert refusal.reason == direct_event_refusal(native_review_owner, path, 2).reason


@pytest.mark.usefixtures(native_review_owner.__name__)
def test_review_preparation_walks_a_hook_complaint_session_under_the_output_bound(tmp_path, settings, event_reads):
    entries = [
        *(entry for turn in range(24) for entry in bulky_turn(f"audit-{turn}", 6)),
        assistant_text(HOOK_COMPLAINT_TEXT),
    ]
    prepared, corrections, refused = prepare_source(
        CURRENT_CLIENT.get(), write_transcript(tmp_path / "s.jsonl", entries), settings, REPO, recorded=nothing_recorded
    )
    assert prepared["disposition"] == "eligible"
    assert (corrections, refused) == ([], [])
    assert event_reads == Counter(range(len(entries)))


@pytest.mark.usefixtures(native_review_owner.__name__)
def test_review_preparation_reads_a_multi_comment_message_once(tmp_path, settings, event_reads):
    prepared, _, refused = prepare_source(
        CURRENT_CLIENT.get(),
        write_transcript(tmp_path / "s.jsonl", review_comments(60)),
        settings,
        REPO,
        recorded=nothing_recorded,
    )
    kinds = Counter(decode_candidate(raw)[0].source_kind for raw in prepared["candidates_json"])
    assert (kinds["review_comment"], refused) == (60, [])
    assert event_reads == Counter([1])


@pytest.mark.usefixtures(native_review_owner.__name__)
def test_review_preparation_refuses_a_candidate_over_the_owned_page_by_size(tmp_path, settings):
    with pytest.raises(EvidenceIncomplete) as refused:
        prepare_source(
            CURRENT_CLIENT.get(),
            write_transcript(tmp_path / "s.jsonl", review_comments(120)),
            settings,
            REPO,
            recorded=nothing_recorded,
        )
    assert refused.value.status == "output_limit"
    assert re.fullmatch(
        r"owned page for record \d+ of candidates_json needs \d+ bytes, over the 1044480-byte reply bound",
        refused.value.reason,
    )


@pytest.mark.usefixtures(native_review_owner.__name__)
async def test_scan_refuses_an_oversized_transcript_whole_and_scans_the_rest(tmp_path, store, settings):
    write_transcript(
        tmp_path / "a.jsonl",
        [
            user_text("run a status check"),
            assistant_tool_use("t1", "Bash", {"command": "cat log"}),
            tool_result("t1", "r" * (1024 * 1024 + 1)),
            assistant_text(HOOK_COMPLAINT_TEXT),
        ],
    )
    kept = write_transcript(tmp_path / "b.jsonl", correction_entries())
    assert await scan(store, settings=settings, transcripts=[tmp_path], repo_key=REPO) == ScanReport(1, 1, 1)
    assert set(await store.file_mtimes()) == {str(kept.resolve())}
    await store.set_meta(f"snapshot_discovery:{dedup_key(str(tmp_path.absolute()))}", None)
    assert await scan(store, settings=settings, transcripts=[tmp_path], repo_key=REPO) == ScanReport(0, 0, 0)
