"""The ``status.json`` snapshot writer: the frozen golden, an empty store, title fallback, atomicity.

The golden test seeds a real review store to the exact fixture inputs behind
``tests/fixtures/status-json-v1.golden.json`` and asserts :func:`write_status` reproduces it
byte-for-byte, pinning the Python writer to the cross-process contract the Swift widget reads.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cc_transcript.ids import SessionId
from cc_transcript.mining.candidates import DedupKey
from cc_transcript.mining.confidence import MEDIUM, CandidateSignal, Confidence, to_payload
from cc_transcript.mining.sourcekind import SourceKind

from captain_hook.review import snapshot as snap
from captain_hook.review.repo import RepoKey
from captain_hook.review.store import CandidateKind, CandidateStatus
from tests.review_helpers import Verdict, install_fake_embedder

if TYPE_CHECKING:
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

REPO = RepoKey("github.com/yasyf/captain-hook")
GOLDEN = Path(__file__).parent / "fixtures" / "status-json-v1.golden.json"

INSERT_EVENT = (
    "INSERT INTO feedback_events (dedup_key, source_kind, session_id, occurred_at, text, payload_json, "
    "context_json, ingested_at) VALUES (?, ?, ?, ?, ?, ?, '{}', '2026-06-01T00:00:00+00:00')"
)


def _medium_payload() -> str:
    return json.dumps({"signal": to_payload(CandidateSignal(Confidence(MEDIUM), ("marker",)))})


def seed(store: ReviewStore, candidate_id: int, key: str, *, session: str, occurred: str) -> None:
    store.store.execute(
        INSERT_EVENT, (key, "transcript_message", session, occurred, f"text {key}", _medium_payload())
    )
    store.record_observation(
        candidate_id,
        dedup_key=DedupKey(key),
        session_id=SessionId(session),
        occurred_at=datetime.fromisoformat(occurred),
    )


async def accept(store: ReviewStore, key: str) -> None:
    cur = store.store.sql("SELECT source_kind FROM feedback_events WHERE dedup_key = ?", (key,))
    store.record_verdict(
        DedupKey(key),
        Verdict(accepted=True, confidence=0.9, canonical_key=None),
        role="judge",
        prompt_version=store.versions.for_row(cur[0]),
        model="m1",
        fidelity="full",
    )


def insert_candidate(store: ReviewStore, rule: str, status: str, **extra: object) -> None:
    cols: dict[str, object] = {
        "repo_key": str(REPO),
        "candidate_kind": "create",
        "rule": rule,
        "source_kind": "transcript_message",
        "status": status,
        "created_at": "2026-07-15T00:00:00+00:00",
        "updated_at": "2026-07-15T00:00:00+00:00",
        **extra,
    }
    placeholders = ",".join("?" for _ in cols)
    store.store.execute(f"INSERT INTO candidates ({','.join(cols)}) VALUES ({placeholders})", tuple(cols.values()))


async def seed_golden(store: ReviewStore) -> None:
    store.enable(REPO)
    eligible = store.ensure_candidate(
        REPO, kind=CandidateKind.CREATE, rule="no-force-push", source_kind=SourceKind("transcript_message")
    )
    for i, (session, day) in enumerate([("s1", "2026-06-01"), ("s2", "2026-06-01"), ("s3", "2026-06-02")]):
        seed(store, eligible, f"k{i}", session=session, occurred=f"{day}T10:00:00+00:00")
        await accept(store, f"k{i}")
    for rule in ("w1", "w2"):
        insert_candidate(store, rule, "watching")
    for rule in ("a1", "a2", "a3", "a4"):
        insert_candidate(store, rule, "accepted")
    for rule in ("r1", "r2", "r3"):
        insert_candidate(store, rule, "rejected")
    insert_candidate(
        store,
        "guard-rm-rf",
        "pr_open",
        id=42,
        pr_url="https://github.com/yasyf/captain-hook/pull/12",
        pr_opened_at="2026-07-15T11:30:00+00:00",
        pr_title="[capt-hook] Block force-pushes",
    )
    store.store.execute(
        "INSERT INTO spawn_runs (started_at, finished_at, transcript, ok, error, report_json) "
        "VALUES (?, ?, ?, 1, NULL, '{}')",
        ("2026-07-15T11:58:00+00:00", "2026-07-15T11:59:00+00:00", "/t.jsonl"),
    )
    for i in range(3):
        store.store.execute(
            INSERT_EVENT,
            (f"jp{i}", "transcript_message", f"js{i}", "2026-06-10T10:00:00+00:00", f"pending {i}", _medium_payload()),
        )


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snap, "_utcnow", lambda: datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(snap, "capt_hook_version", lambda: "9.4.0")


async def test_write_status_matches_golden(
    store: ReviewStore, settings: ReviewSettings, pinned: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_embedder(monkeypatch)
    await seed_golden(store)
    path = snap.write_status(store, settings=settings)
    assert path == snap.status_path()
    assert path.read_bytes() == GOLDEN.read_bytes()


async def test_build_snapshot_matches_golden_dict(
    store: ReviewStore, settings: ReviewSettings, pinned: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_embedder(monkeypatch)
    await seed_golden(store)
    assert snap.build_snapshot(store, settings=settings) == json.loads(GOLDEN.read_text())


def test_empty_store_snapshot(store: ReviewStore, settings: ReviewSettings, pinned: None) -> None:
    path = snap.write_status(store, settings=settings)
    assert path.read_bytes() == (
        b'{"schema_version":1,"generated_at":"2026-07-15T12:00:00Z","capt_hook_version":"9.4.0",'
        b'"repos":[],"health":{"ok":true,"consecutive_failures":0,"failing_since":null,'
        b'"last_run_at":null,"judge_pending":0}}\n'
    )


def test_pr_title_falls_back_to_description(store: ReviewStore, settings: ReviewSettings) -> None:
    store.enable(REPO)
    insert_candidate(
        store,
        "guard-rm-rf",
        "pr_open",
        pr_url="https://github.com/yasyf/captain-hook/pull/9",
        pr_opened_at="2026-07-15T11:30:00+00:00",
    )
    [repo] = snap.build_snapshot(store, settings=settings)["repos"]
    [entry] = repo["open_prs"]
    assert entry["title"] == "would add a hook for this correction"


def test_write_status_replaces_atomically(store: ReviewStore, settings: ReviewSettings) -> None:
    path = snap.write_status(store, settings=settings)
    first = path.read_bytes()
    store.enable(REPO)
    again = snap.write_status(store, settings=settings)
    assert again == path
    assert again.read_bytes() != first  # overwritten in place, not appended
    assert list(path.parent.glob(".status-*.json")) == []  # no orphaned tempfiles


def test_urlless_pr_open_row_stays_schema_valid(store: ReviewStore, settings: ReviewSettings) -> None:
    store.enable(REPO)
    insert_candidate(store, "guard-rm-rf", "pr_open")
    [repo] = snap.build_snapshot(store, settings=settings)["repos"]
    [entry] = repo["open_prs"]
    assert entry["url"] == ""
    assert entry["opened_at"] == "2026-07-15T00:00:00Z"  # legacy NULL falls back to updated_at


def test_transition_to_pr_open_stamps_opened_at(store: ReviewStore) -> None:
    store.enable(REPO)
    insert_candidate(store, "w-stamp", "watching", id=77)
    assert store.transition(77, CandidateStatus.PR_OPEN)
    row = store.store.sql("SELECT pr_opened_at FROM candidates WHERE id = 77")[0]
    assert row["pr_opened_at"] is not None
