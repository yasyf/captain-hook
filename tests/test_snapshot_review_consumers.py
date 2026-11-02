from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cc_transcript.context import ContextWindow
from cc_transcript.ids import EventRef, EventUuid, SessionId
from cc_transcript.mining.candidates import dedup_key
from spawnllm import Decision, LabelAnswer, Refused

from captain_hook.review.judge import CONTEXT_BUDGET, TRIGGER_BUDGET, prompt_builder
from captain_hook.review.scan import (
    CorrectionLedger,
    PreparedSource,
    ReviewState,
    ScanReport,
    ingest,
    prepare_source,
    scan,
)
from captain_hook.snapshots.client import CURRENT_CLIENT, EvidenceIncomplete, SnapshotClient
from captain_hook.snapshots.review import RenderedEvidence, ReviewPolicy, render_review_windows
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


async def nothing_recorded(anchors) -> set[EventRef]:
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
        "model": None,
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
                    "prepared_cache_reads",
                    "prepared_cache_bytes_read",
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
            [window("a"), window("b"), window("a")],
            source_paths=[None, None, None],
            roots=["/fixtures"],
            render=render_budget(),
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
            [window("bad"), window("good"), window("gone")],
            source_paths=[None, None, None],
            roots=["/fixtures"],
            render=render_budget(),
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
        [result] = render_review_windows([window()], source_paths=[None], roots=["/fixtures"], render=render_budget())
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


async def test_source_preparation_releases_lease_on_incomplete(settings):
    released = []
    session = SimpleNamespace(release=lambda: released.append(True))

    def review_events(*args, **kwargs):
        raise EvidenceIncomplete("output_limit", "bounded")

    client = SimpleNamespace(acquire=lambda path: session, review_events=review_events)
    with pytest.raises(EvidenceIncomplete):
        await prepare_source(
            client, Path("/unused"), settings, ReviewState(), policy=ReviewPolicy(), recorded=nothing_recorded
        )
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


@pytest.mark.parametrize("status", ["output_limit", "source_limit", "entry_limit"])
async def test_oversized_source_is_refused_and_the_scan_completes(store, settings, monkeypatch, status):
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
        lambda *args, **kwargs: (_ for _ in ()).throw(EvidenceIncomplete(status, "source exceeds owner bound")),
    )
    try:
        report = await scan(store, settings=settings, transcripts=[Path("/fixture")])
    finally:
        CURRENT_CLIENT.reset(token)
    assert report == ScanReport(0, 0, 1)
    assert await store.meta(f"snapshot_refused:{dedup_key('/fixture/session.jsonl')}") == "r1"
    assert await store.meta(f"snapshot_discovery:{dedup_key('/fixture')}") == "next"


async def test_ingest_records_empty_source_atomically(store):
    prepared = PreparedSource(
        canonical_path="/fixture/source.jsonl",
        mtime_ns=1_230_000_000,
        repo_key=REPO,
        state=ReviewState(),
        caught_up=True,
    )
    async with CorrectionLedger() as ledger:
        assert await ingest(store, prepared, ledger=ledger) == ScanReport(1, 0)
    assert await store.file_mtimes() == {"/fixture/source.jsonl": 1.23}


async def test_ingest_failure_rolls_back_source_and_feedback(store, monkeypatch):
    from captain_hook.review.scan import detect, rule_parts, to_candidate

    signal = next(detect(parse(correction_entries())))
    prepared = PreparedSource(
        canonical_path="/fixture/source.jsonl",
        mtime_ns=1_230_000_000,
        repo_key=REPO,
        state=ReviewState(),
        caught_up=True,
        candidates=((to_candidate(window(), signal), rule_parts(signal)),),
    )
    monkeypatch.setattr(store, "record_observation", AsyncMock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        await ingest(store, prepared, ledger=CorrectionLedger())
    assert await store.file_mtimes() == {}
    assert await store.db.sql("SELECT dedup_key FROM feedback_events") == []


@pytest.mark.parametrize(
    ("jev", "recorded_rows", "llm_picks"),
    [("1", 1, False), ("none", 0, False), (TimeoutError("no jev"), 1, True), (Refused(), 1, True)],
)
async def test_correction_drafts_keep_full_hunks(monkeypatch, jev, recorded_rows, llm_picks):
    from cc_transcript.extract.correct import CorrectionPick

    from captain_hook.snapshots.review import correction_drafts, record_correction_drafts

    old = "x" * 3000
    new = "y" * 3000
    entries = [
        assistant_tool_use("edit", "Edit", {"file_path": "x.py", "old_string": old, "new_string": new}),
        user_text(CORRECTION, uuid="feedback"),
    ]
    drafts, refused = correction_drafts(
        parse(entries),
        [SimpleNamespace(ref=EventRef(SessionId("sess-1"), EventUuid("feedback")), text=CORRECTION)],
        None,
    )
    assert refused == []
    recorded = []

    class Log:
        async def for_anchor(self, *args):
            return []

        async def append(self, row):
            recorded.append(row)

    chosen_by_llm = []

    async def choose(prompt, schema, **kwargs):
        assert CORRECTION in prompt
        chosen_by_llm.append(prompt)
        return CorrectionPick(candidate=1, note="fixture")

    async def decide(state, questions, **kwargs):
        assert CORRECTION in state
        assert list(questions["candidate"].options) == ["none", "1"]
        if isinstance(jev, BaseException):
            raise jev
        answer = jev if isinstance(jev, Refused) else LabelAnswer(jev, {jev: 0.9}, 0.8)
        return Decision({"candidate": answer}, "jev-1.13.0", 120, 95.0)

    monkeypatch.setattr("cc_transcript.extract.correct.usable_backend", lambda: object())
    monkeypatch.setattr("spawnllm.extract", choose)
    monkeypatch.setattr("spawnllm.decide", decide)
    await record_correction_drafts(drafts, ledger=SimpleNamespace(handle=AsyncMock(return_value=Log())))
    assert bool(chosen_by_llm) is llm_picks
    assert len(recorded) == recorded_rows
    if recorded_rows:
        assert recorded[0].incorrect_old == old
        assert recorded[0].incorrect_new == new
        assert recorded[0].incorrect_digest


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
    with pytest.raises(EvidenceIncomplete, match="work budget"):
        BoundedGit(max_bytes=0)(Path("/fixture"), "show", "HEAD")


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
    with pytest.raises(EvidenceIncomplete, match="Git output"):
        BoundedGit(max_bytes=10)(Path("/fixture"), "show", "HEAD")
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


@pytest.fixture
def git_calls(monkeypatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    class CountingGit:
        def __init__(self, *, max_bytes, max_processes=84):
            pass

        def __call__(self, repo, *arguments):
            calls.append(arguments)

    monkeypatch.setattr("captain_hook.snapshots.review.BoundedGit", CountingGit)
    return calls


@pytest.fixture
def prepared_batches(monkeypatch) -> list[list[str]]:
    from captain_hook.snapshots import review as review_module

    batches: list[list[str]] = []
    draft = review_module.correction_drafts

    def counted(events, candidates, repo):
        batches.append([candidate.ref.event_uuid for candidate in candidates])
        return draft(events, candidates, repo)

    monkeypatch.setattr(review_module, "correction_drafts", counted)
    return batches


def anchor_uuid(edit: list[dict]) -> str:
    return edit[-1]["uuid"]


async def prepare(
    path: Path, settings, *, state: ReviewState | None = None, recorded=nothing_recorded
) -> PreparedSource:
    policy = ReviewPolicy()
    try:
        return await prepare_source(
            CURRENT_CLIENT.get(), path, settings, state or ReviewState(), REPO, policy=policy, recorded=recorded
        )
    finally:
        await policy.close()


def corrected(prepared: PreparedSource) -> Counter[str]:
    return Counter(
        candidate.ref.event_uuid
        for candidate, _ in prepared.candidates
        if candidate.source_kind == "transcript_message"
    )


async def prepared_anchors(path: Path, settings) -> tuple[Counter[str], Counter[str], list[tuple[EventRef, Any]]]:
    prepared = await prepare(path, settings)
    drafted = Counter(draft["anchor"]["event_uuid"] for draft in prepared.corrections)
    return corrected(prepared), drafted, list(prepared.refused)


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
async def test_a_correction_recorded_under_another_anchor_ref_leaves_the_anchor_prepared(
    tmp_path, settings, prepared_batches
):
    from dataclasses import replace

    from cc_transcript.corrections import Correction, CorrectionLog

    parser, lexer = corrected_edit("parser"), corrected_edit("lexer")
    path = write_transcript(tmp_path / "s.jsonl", [*parser, *lexer])
    rows = {
        draft["anchor"]["event_uuid"]: Correction(**json.loads(draft["choices"][0]["correction_json"]))
        for draft in (await prepare(path, settings)).corrections
    }
    async with await CorrectionLog.open() as log:
        await log.append(rows[anchor_uuid(parser)])
        await log.append(replace(rows[anchor_uuid(lexer)], anchor_uuid=EventUuid("superseded-anchor")))
    prepared_batches.clear()
    prepared = await prepare(path, settings, recorded=recorded_in_ledger)
    assert prepared_batches == [[anchor_uuid(lexer)]]
    assert [draft["anchor"]["event_uuid"] for draft in prepared.corrections] == [anchor_uuid(lexer)]


@pytest.mark.usefixtures(native_review_owner.__name__)
async def test_a_second_pass_mines_only_events_past_the_watermark(tmp_path, settings):
    parser, lexer = corrected_edit("parser"), corrected_edit("lexer")
    path = write_transcript(tmp_path / "s.jsonl", parser)
    first = await prepare(path, settings)
    write_transcript(path, [*parser, *lexer])
    second = await prepare(path, settings, state=first.state)
    assert (corrected(first), corrected(second)) == (Counter([anchor_uuid(parser)]), Counter([anchor_uuid(lexer)]))
    assert (first.caught_up, second.caught_up) == (True, True)
    assert second.state.watermark != first.state.watermark


@pytest.mark.usefixtures(native_review_owner.__name__)
async def test_a_rewritten_transcript_resets_its_watermark_and_is_mined_whole(tmp_path, settings):
    parser, lexer = corrected_edit("parser"), corrected_edit("lexer")
    path = write_transcript(tmp_path / "s.jsonl", [*parser, *lexer])
    first = await prepare(path, settings)
    path.unlink()
    write_transcript(path, [*lexer, *parser])
    second = await prepare(path, settings, state=first.state)
    assert corrected(second) == Counter([anchor_uuid(parser), anchor_uuid(lexer)])


@pytest.mark.usefixtures(native_review_owner.__name__)
async def test_a_scan_pages_a_long_transcript_across_passes_until_caught_up(
    tmp_path, store, settings, prepared_batches, monkeypatch
):
    from captain_hook.review import scan as scan_module

    monkeypatch.setattr(scan_module, "REVIEW_PASS_PAGES", 1)
    parser, lexer = corrected_edit("parser"), corrected_edit("lexer")
    path = write_transcript(
        tmp_path / "s.jsonl", [*parser, *(user_text(f"status {turn}") for turn in range(300)), *lexer]
    )
    assert (await scan(store, settings=settings, transcripts=[tmp_path], repo_key=REPO)).scanned == 1
    assert prepared_batches == [[anchor_uuid(parser)], [anchor_uuid(lexer)]]
    assert ReviewState.load(await store.meta(f"snapshot_review:{dedup_key(str(path))}")).watermark is not None


@pytest.mark.usefixtures(native_review_owner.__name__)
async def test_corrections_cover_an_anchor_after_a_turn_larger_than_one_record(tmp_path, settings):
    entries = [*bulky_turn("audit", 16), *corrected_edit("parser")]
    eligible, drafted, refused = await prepared_anchors(write_transcript(tmp_path / "s.jsonl", entries), settings)
    assert len(eligible) == 1
    assert (drafted, refused) == (eligible, [])


@pytest.mark.usefixtures(native_review_owner.__name__)
async def test_corrections_cover_anchors_whose_windows_together_exceed_one_page(tmp_path, settings):
    entries = [
        *(entry for turn in range(6) for entry in bulky_turn(f"audit-{turn}", 5)),
        *(entry for label in ("parser", "lexer", "loader", "writer", "reader") for entry in corrected_edit(label)),
    ]
    eligible, drafted, refused = await prepared_anchors(write_transcript(tmp_path / "s.jsonl", entries), settings)
    assert len(eligible) == 5
    assert (drafted, refused) == (eligible, [])


@pytest.mark.usefixtures(native_review_owner.__name__)
async def test_an_event_over_the_record_bound_is_skipped_and_its_neighbors_reviewed(tmp_path, settings):
    path = write_transcript(
        tmp_path / "s.jsonl",
        [
            user_text("run the audit"),
            assistant_tool_use("big", "Bash", {"command": "cat log"}),
            tool_result("big", "r" * (1024 * 1024 + 1)),
            *corrected_edit("parser"),
        ],
    )
    eligible, drafted, refused = await prepared_anchors(path, settings)
    assert len(eligible) == 1
    assert (drafted, refused) == (eligible, [])


@pytest.mark.usefixtures(native_review_owner.__name__)
async def test_review_preparation_keeps_every_comment_of_a_multi_comment_message(tmp_path, settings):
    prepared = await prepare(write_transcript(tmp_path / "s.jsonl", review_comments(60)), settings)
    kinds = Counter(candidate.source_kind for candidate, _ in prepared.candidates)
    assert (kinds["review_comment"], prepared.refused) == (60, ())


@pytest.mark.usefixtures(native_review_owner.__name__)
async def test_scan_reviews_a_transcript_around_an_oversized_event_and_scans_the_rest(tmp_path, store, settings):
    oversized = write_transcript(
        tmp_path / "a.jsonl",
        [
            user_text("run a status check", sessionId="a"),
            assistant_tool_use("t1", "Bash", {"command": "cat log"}, sessionId="a"),
            tool_result("t1", "r" * (1024 * 1024 + 1), sessionId="a"),
            *correction_entries(session="a"),
        ],
    )
    kept = write_transcript(tmp_path / "b.jsonl", correction_entries(session="b"))
    assert await scan(store, settings=settings, transcripts=[tmp_path], repo_key=REPO) == ScanReport(2, 2, 0)
    assert set(await store.file_mtimes()) == {str(oversized.resolve()), str(kept.resolve())}
    await store.set_meta(f"snapshot_discovery:{dedup_key(str(tmp_path.absolute()))}", None)
    assert await scan(store, settings=settings, transcripts=[tmp_path], repo_key=REPO) == ScanReport(0, 0, 0)


@pytest.mark.parametrize("unrelated", [0, 32, 256])
async def test_judge_known_source_avoids_unrelated_corpus_metadata(
    unrelated, store, settings, tmp_path, monkeypatch, native_review_owner
):
    from captain_hook.review.judge import judge_pass
    from captain_hook.review.scan import scan_transcript
    from captain_hook.snapshots import review as review_module
    from tests.review_helpers import install_fake_embedder, install_judge, install_resolved_model

    for index in range(unrelated):
        write_transcript(tmp_path / f"unrelated-{index}.jsonl", correction_entries(session=f"other-{index}"))
    selected = write_transcript(tmp_path / "explicit-source.jsonl", correction_entries(session="selected"))
    await scan_transcript(store, selected, settings=settings, repo_key=REPO)
    install_fake_embedder(monkeypatch)
    calls = install_judge(monkeypatch, category="durable_style_rule")
    install_resolved_model(monkeypatch)
    operations = []
    usage = []
    exchange = native_review_owner.client._exchange

    def counted(envelope):
        result = exchange(envelope)
        operations.append(envelope["request"]["operation"])
        usage.append(result["response"]["usage"])
        return result

    file_roster_reads = []
    root_normalizations = []
    file_mtimes = store.file_mtimes
    review_roots = review_module.review_roots

    async def counted_roster():
        file_roster_reads.append(True)
        return await file_mtimes()

    def counted_roots(paths):
        root_normalizations.append(True)
        return review_roots(paths)

    monkeypatch.setattr(store, "file_mtimes", counted_roster)
    monkeypatch.setattr(review_module, "review_roots", counted_roots)
    monkeypatch.setattr(native_review_owner.client, "_exchange", counted)
    report = await judge_pass(store, settings=settings)
    examined = sum(item["discovery_entries_examined"] for item in usage)
    assert examined == 0
    assert file_roster_reads == []
    assert root_normalizations == []
    assert "resolve" not in operations
    assert report.judged == len(calls) == 1
    judged = await store.db.sql("SELECT fidelity FROM verdicts ORDER BY id")
    assert [row["fidelity"] for row in judged] == ["full"]
