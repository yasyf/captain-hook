from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from cc_transcript.ids import SessionId
from cc_transcript.mining.candidates import DedupKey
from cc_transcript.mining.confidence import MEDIUM, VERY_HIGH, CandidateSignal, Confidence, to_payload
from cc_transcript.mining.sourcekind import SourceKind

from captain_hook.review.repo import RepoKey
from captain_hook.review.settings import ReviewSettings
from captain_hook.review.store import (
    TRANSITIONS,
    CandidateKind,
    CandidateStatus,
    InvalidTransition,
    ReviewStore,
)

if TYPE_CHECKING:
    from cc_transcript.context import Fidelity

REPO = RepoKey("github.com/yasyf/captain-hook")
PROMPT_VERSION = 1


def digest_rule(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


INSERT_EVENT = (
    "INSERT INTO feedback_events (dedup_key, source_kind, session_id, occurred_at, text, payload_json, "
    "context_json, ingested_at) VALUES (?, 'transcript_message', ?, ?, ?, ?, '{}', '2026-06-01T00:00:00+00:00')"
)

ALLOWED_MOVES = {
    (CandidateStatus.WATCHING, CandidateStatus.PR_OPEN),
    (CandidateStatus.WATCHING, CandidateStatus.REJECTED),
    (CandidateStatus.PR_OPEN, CandidateStatus.STALE),
    (CandidateStatus.PR_OPEN, CandidateStatus.ACCEPTED),
    (CandidateStatus.PR_OPEN, CandidateStatus.REJECTED),
    (CandidateStatus.STALE, CandidateStatus.ACCEPTED),
    (CandidateStatus.STALE, CandidateStatus.REJECTED),
}

PATHS: dict[CandidateStatus, tuple[CandidateStatus, ...]] = {
    CandidateStatus.WATCHING: (),
    CandidateStatus.PR_OPEN: (CandidateStatus.PR_OPEN,),
    CandidateStatus.STALE: (CandidateStatus.PR_OPEN, CandidateStatus.STALE),
    CandidateStatus.ACCEPTED: (CandidateStatus.PR_OPEN, CandidateStatus.ACCEPTED),
    CandidateStatus.REJECTED: (CandidateStatus.PR_OPEN, CandidateStatus.REJECTED),
}


@dataclass(frozen=True, slots=True)
class Verdict:
    accepted: bool = True
    confidence: float = 0.9
    category: str = "durable_correction"
    summary: str = "user corrected approach"
    rationale: str = "explicit correction"
    canonical_key: str | None = None


async def create_candidate(store: ReviewStore, *, rule: str = "no-force-push") -> int:
    return await store.ensure_candidate(
        REPO, kind=CandidateKind.CREATE, rule=rule, source_kind=SourceKind("transcript_message")
    )


async def fix_candidate(
    store: ReviewStore, *, hook: str = "hooks.style:nudge_1", file: str = ".claude/hooks/style.py"
) -> int:
    return await store.ensure_candidate(
        REPO,
        kind=CandidateKind.FIX,
        rule="misfire",
        source_kind=SourceKind("hook_complaint"),
        target_source_file=file,
        target_hook_name=hook,
    )


async def seed(
    store: ReviewStore, candidate_id: int, key: str, *, session: str, occurred: str, heuristic: float = MEDIUM
) -> None:
    payload = json.dumps({"signal": to_payload(CandidateSignal(Confidence(heuristic), ("marker",)))})
    await store.store.conn.execute(INSERT_EVENT, (key, session, occurred, f"text {key}", payload))
    await store.record_observation(
        candidate_id,
        dedup_key=DedupKey(key),
        session_id=SessionId(session),
        occurred_at=datetime.fromisoformat(occurred),
    )


async def judge(
    store: ReviewStore,
    key: str,
    *,
    accepted: bool = True,
    confidence: float = 0.9,
    model: str = "m1",
    slug: str | None = None,
    fidelity: Fidelity = "full",
) -> None:
    await store.record_verdict(
        DedupKey(key),
        Verdict(accepted=accepted, confidence=confidence, canonical_key=slug),
        role="judge",
        prompt_version=PROMPT_VERSION,
        model=model,
        fidelity=fidelity,
    )


async def seed_merge_pair(store: ReviewStore, *, slug: str = "shared-slug") -> tuple[int, int]:
    first = await create_candidate(store, rule=digest_rule("a"))
    second = await create_candidate(store, rule=digest_rule("b"))
    await seed(store, first, "ka", session="s1", occurred="2026-06-01T10:00:00+00:00")
    await seed(store, second, "kb", session="s2", occurred="2026-06-02T10:00:00+00:00")
    await judge(store, "ka", slug=slug)
    await judge(store, "kb", slug=slug)
    return first, second


async def dump_table(store: ReviewStore, table: str) -> list[dict[str, object]]:
    cur = await store.store.conn.execute(f"SELECT * FROM {table} ORDER BY id")
    return [dict(row) async for row in cur]


async def eligible_create_candidate(store: ReviewStore) -> int:
    candidate_id = await create_candidate(store)
    for i, (session, day) in enumerate([("s1", "2026-06-01"), ("s2", "2026-06-01"), ("s3", "2026-06-02")]):
        await seed(store, candidate_id, f"k{i}", session=session, occurred=f"{day}T10:00:00+00:00")
        await judge(store, f"k{i}")
    return candidate_id


async def open_pr(store: ReviewStore, *, rule: str, opened_at: datetime) -> int:
    candidate_id = await create_candidate(store, rule=rule)
    await store.transition(
        candidate_id, CandidateStatus.PR_OPEN, pr_url=f"https://github.com/x/y/pull/{rule}", pr_opened_at=opened_at
    )
    return candidate_id


async def candidate_row(store: ReviewStore, candidate_id: int) -> dict[str, object]:
    cur = await store.store.conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
    return [dict(row) async for row in cur][0]


class TestSchema:
    async def test_open_layers_feedback_verdicts_and_review_tables(self, store: ReviewStore) -> None:
        cur = await store.store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        names = {str(row["name"]) async for row in cur}
        assert {"files", "feedback_events", "verdicts", "candidates", "candidate_observations", "repos"} <= names

    def test_generic_verdict_names(self) -> None:
        assert (ReviewStore.VERDICT_TABLE, ReviewStore.ACCEPTED_COLUMN, ReviewStore.SUMMARY_COLUMN) == (
            "verdicts",
            "accepted",
            "summary",
        )


class TestRepos:
    async def test_watching_roundtrip(self, store: ReviewStore) -> None:
        assert await store.watching(REPO) is False
        await store.enable(REPO)
        assert await store.watching(REPO) is True
        await store.disable(REPO)
        assert await store.watching(REPO) is False
        await store.enable(REPO)
        assert await store.watching(REPO) is True


class TestCandidates:
    async def test_ensure_create_candidate_groups_by_repo_and_rule(self, store: ReviewStore) -> None:
        first = await create_candidate(store, rule="no-force-push")
        assert await create_candidate(store, rule="no-force-push") == first
        assert await create_candidate(store, rule="prefer-uv-run") != first
        other_repo = await store.ensure_candidate(
            RepoKey("github.com/other/repo"),
            kind=CandidateKind.CREATE,
            rule="no-force-push",
            source_kind=SourceKind("transcript_message"),
        )
        assert other_repo != first
        assert (await candidate_row(store, first))["status"] == "watching"

    async def test_ensure_fix_candidate_groups_by_target(self, store: ReviewStore) -> None:
        first = await fix_candidate(store)
        again = await store.ensure_candidate(
            REPO,
            kind=CandidateKind.FIX,
            rule="different-rule",
            source_kind=SourceKind("hook_complaint"),
            target_source_file=".claude/hooks/style.py",
            target_hook_name="hooks.style:nudge_1",
            misfire_class="refire_on_own_text",
        )
        assert again == first
        assert await fix_candidate(store, file=".claude/hooks/other.py") != first

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param(
                {
                    "kind": CandidateKind.CREATE,
                    "rule": "r",
                    "source_kind": SourceKind("transcript_message"),
                    "target_hook_name": "hooks.style:nudge_1",
                    "target_source_file": ".claude/hooks/style.py",
                },
                id="create-rejects-fix-only-fields",
            ),
            pytest.param(
                {"kind": CandidateKind.FIX, "rule": "r", "source_kind": SourceKind("hook_complaint")},
                id="fix-requires-targets",
            ),
        ],
    )
    async def test_ensure_candidate_rejects_invalid_target_shape(
        self, store: ReviewStore, kwargs: dict[str, object]
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            await store.ensure_candidate(REPO, **kwargs)


class TestTransitions:
    def test_terminal_states_have_no_moves(self) -> None:
        assert TRANSITIONS[CandidateStatus.ACCEPTED] == frozenset()
        assert TRANSITIONS[CandidateStatus.REJECTED] == frozenset()

    @pytest.mark.parametrize(
        ("frm", "to"),
        [(frm, to) for frm in CandidateStatus for to in CandidateStatus],
        ids=[f"{frm}-to-{to}" for frm in CandidateStatus for to in CandidateStatus],
    )
    async def test_transition_matrix(self, store: ReviewStore, frm: CandidateStatus, to: CandidateStatus) -> None:
        candidate_id = await create_candidate(store)
        for step in PATHS[frm]:
            await store.transition(candidate_id, step, pr_opened_at=datetime.now(UTC))
        if (frm, to) in ALLOWED_MOVES:
            await store.transition(candidate_id, to, pr_opened_at=datetime.now(UTC))
            assert (await candidate_row(store, candidate_id))["status"] == str(to)
        else:
            with pytest.raises(InvalidTransition, match=f"{frm} -> {to}"):
                await store.transition(candidate_id, to)
            assert (await candidate_row(store, candidate_id))["status"] == str(frm)

    async def test_pr_open_stamps_url_and_kept_through_acceptance(self, store: ReviewStore) -> None:
        candidate_id = await create_candidate(store)
        opened_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        await store.transition(
            candidate_id, CandidateStatus.PR_OPEN, pr_url="https://github.com/x/y/pull/1", pr_opened_at=opened_at
        )
        row = await candidate_row(store, candidate_id)
        assert row["pr_url"] == "https://github.com/x/y/pull/1"
        assert row["pr_opened_at"] == "2026-06-01T12:00:00+00:00"
        await store.transition(candidate_id, CandidateStatus.ACCEPTED)
        row = await candidate_row(store, candidate_id)
        assert (row["status"], row["pr_url"]) == ("accepted", "https://github.com/x/y/pull/1")
        assert row["pr_opened_at"] == "2026-06-01T12:00:00+00:00"

    async def test_transition_unknown_candidate_raises(self, store: ReviewStore) -> None:
        with pytest.raises(LookupError, match="no candidate with id 999"):
            await store.transition(999, CandidateStatus.PR_OPEN)


class TestCreateEligibility:
    async def test_unjudged_observations_never_count(self, store: ReviewStore, settings: ReviewSettings) -> None:
        await store.enable(REPO)
        candidate_id = await create_candidate(store)
        for i, day in enumerate(("2026-06-01", "2026-06-02", "2026-06-03")):
            await seed(store, candidate_id, f"k{i}", session=f"s{i}", occurred=f"{day}T10:00:00+00:00")
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.sessions, status.days) == (0, 0)
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is False

    @pytest.mark.parametrize(
        ("accepted", "confidence", "counts"),
        [
            pytest.param(False, 0.9, False, id="judge-rejected"),
            pytest.param(True, 0.59, False, id="accepted-below-min-judge-confidence"),
            pytest.param(True, 0.6, True, id="accepted-at-min-judge-confidence"),
            pytest.param(True, 0.9, True, id="accepted-above-min-judge-confidence"),
        ],
    )
    async def test_only_confident_judge_acceptance_counts(
        self, store: ReviewStore, settings: ReviewSettings, accepted: bool, confidence: float, counts: bool
    ) -> None:
        await store.enable(REPO)
        candidate_id = await create_candidate(store)
        for i, day in enumerate(("2026-06-01", "2026-06-02", "2026-06-03")):
            await seed(store, candidate_id, f"k{i}", session=f"s{i}", occurred=f"{day}T10:00:00+00:00")
            await judge(store, f"k{i}", accepted=accepted, confidence=confidence)
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.sessions, status.days) == ((3, 3) if counts else (0, 0))
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is counts

    async def test_three_observations_in_one_session_are_one_session(
        self, store: ReviewStore, settings: ReviewSettings
    ) -> None:
        await store.enable(REPO)
        candidate_id = await create_candidate(store)
        for i, day in enumerate(("2026-06-01", "2026-06-02", "2026-06-03")):
            await seed(store, candidate_id, f"k{i}", session="s1", occurred=f"{day}T10:00:00+00:00")
            await judge(store, f"k{i}")
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.sessions, status.days) == (1, 3)
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is False

    async def test_three_sessions_on_one_utc_day_are_one_day(
        self, store: ReviewStore, settings: ReviewSettings
    ) -> None:
        await store.enable(REPO)
        candidate_id = await create_candidate(store)
        for i in range(3):
            await seed(store, candidate_id, f"k{i}", session=f"s{i}", occurred=f"2026-06-01T1{i}:00:00+00:00")
            await judge(store, f"k{i}")
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.sessions, status.days) == (3, 1)
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is False

    async def test_days_count_in_utc(self, store: ReviewStore, settings: ReviewSettings) -> None:
        await store.enable(REPO)
        candidate_id = await create_candidate(store)
        await seed(store, candidate_id, "k0", session="s0", occurred="2026-06-01T10:00:00+00:00")
        await seed(store, candidate_id, "k1", session="s1", occurred="2026-06-02T01:00:00+03:00")
        await judge(store, "k0")
        await judge(store, "k1")
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.sessions, status.days) == (2, 1)

    async def test_eligible_when_sessions_and_days_cross_thresholds(
        self, store: ReviewStore, settings: ReviewSettings
    ) -> None:
        await store.enable(REPO)
        candidate_id = await eligible_create_candidate(store)
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.kind, status.watching) == (CandidateKind.CREATE, True)
        assert (status.sessions, status.days, status.open_prs) == (3, 2, 0)
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is True

    @pytest.mark.parametrize(
        "terminal",
        [CandidateStatus.PR_OPEN, CandidateStatus.ACCEPTED, CandidateStatus.REJECTED],
        ids=lambda s: s.value,
    )
    async def test_non_watching_status_never_eligible(
        self, store: ReviewStore, settings: ReviewSettings, terminal: CandidateStatus
    ) -> None:
        await store.enable(REPO)
        candidate_id = await eligible_create_candidate(store)
        await store.transition(candidate_id, CandidateStatus.PR_OPEN, pr_opened_at=datetime.now(UTC))
        if terminal != CandidateStatus.PR_OPEN:
            await store.transition(candidate_id, terminal)
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.status, status.sessions, status.days) == (terminal, 3, 2)
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is False

    async def test_unwatched_repo_never_eligible(self, store: ReviewStore, settings: ReviewSettings) -> None:
        candidate_id = await eligible_create_candidate(store)
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is False
        await store.enable(REPO)
        await store.disable(REPO)
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is False

    @pytest.mark.parametrize(
        ("first", "second", "sessions"),
        [
            pytest.param(True, False, 1, id="first-full-acceptance-wins"),
            pytest.param(False, True, 0, id="first-full-rejection-wins"),
        ],
    )
    async def test_first_full_verdict_wins_regardless_of_model(
        self, store: ReviewStore, settings: ReviewSettings, first: bool, second: bool, sessions: int
    ) -> None:
        await store.enable(REPO)
        candidate_id = await create_candidate(store)
        await seed(store, candidate_id, "k0", session="s0", occurred="2026-06-01T10:00:00+00:00")
        await judge(store, "k0", accepted=first, model="m1")
        await judge(store, "k0", accepted=second, model="m2")
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert status.sessions == sessions

    async def test_other_prompt_version_verdicts_never_count(
        self, store: ReviewStore, settings: ReviewSettings
    ) -> None:
        await store.enable(REPO)
        candidate_id = await eligible_create_candidate(store)
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION + 1)
        assert (status.sessions, status.days) == (0, 0)
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION + 1) is False

    async def test_record_observation_is_idempotent(self, store: ReviewStore, settings: ReviewSettings) -> None:
        await store.enable(REPO)
        candidate_id = await create_candidate(store)
        await seed(store, candidate_id, "k0", session="s0", occurred="2026-06-01T10:00:00+00:00")
        await store.record_observation(
            candidate_id,
            dedup_key=DedupKey("k0"),
            session_id=SessionId("s0"),
            occurred_at=datetime.fromisoformat("2026-06-01T10:00:00+00:00"),
        )
        await judge(store, "k0")
        cur = await store.store.conn.execute("SELECT COUNT(*) AS n FROM candidate_observations")
        assert [int(row["n"]) async for row in cur] == [1]
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.sessions, status.days) == (1, 1)


class TestPrCap:
    async def test_cap_blocks_when_open_prs_reach_max(self, store: ReviewStore, settings: ReviewSettings) -> None:
        await store.enable(REPO)
        candidate_id = await eligible_create_candidate(store)
        await open_pr(store, rule="other-a", opened_at=datetime.now(UTC))
        await open_pr(store, rule="other-b", opened_at=datetime.now(UTC))
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert status.open_prs == 2
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is False

    async def test_stale_transition_frees_a_slot(self, store: ReviewStore, settings: ReviewSettings) -> None:
        await store.enable(REPO)
        candidate_id = await eligible_create_candidate(store)
        stale_one = await open_pr(store, rule="other-a", opened_at=datetime.now(UTC))
        await open_pr(store, rule="other-b", opened_at=datetime.now(UTC))
        await store.transition(stale_one, CandidateStatus.STALE)
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert status.open_prs == 1
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is True

    async def test_pr_older_than_stale_after_days_frees_a_slot(
        self, store: ReviewStore, settings: ReviewSettings
    ) -> None:
        await store.enable(REPO)
        candidate_id = await eligible_create_candidate(store)
        await open_pr(store, rule="other-a", opened_at=datetime.now(UTC) - timedelta(days=31))
        await open_pr(store, rule="other-b", opened_at=datetime.now(UTC))
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert status.open_prs == 1
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is True

    async def test_other_repos_prs_never_count(self, store: ReviewStore, settings: ReviewSettings) -> None:
        await store.enable(REPO)
        candidate_id = await eligible_create_candidate(store)
        for rule in ("other-a", "other-b"):
            other = await store.ensure_candidate(
                RepoKey("github.com/other/repo"),
                kind=CandidateKind.CREATE,
                rule=rule,
                source_kind=SourceKind("transcript_message"),
            )
            await store.transition(other, CandidateStatus.PR_OPEN, pr_opened_at=datetime.now(UTC))
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert status.open_prs == 0
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is True


class TestFixEligibility:
    @pytest.mark.parametrize(
        ("heuristic", "accepted", "judge_confidence", "expected"),
        [
            pytest.param(VERY_HIGH, True, 0.9, True, id="very-high-and-judge-accepted"),
            pytest.param(VERY_HIGH, None, 0.9, False, id="very-high-but-unjudged"),
            pytest.param(VERY_HIGH, False, 0.9, False, id="very-high-but-judge-rejected"),
            pytest.param(VERY_HIGH, True, 0.5, False, id="very-high-but-below-min-judge-confidence"),
            pytest.param(MEDIUM, True, 0.9, False, id="judge-accepted-but-only-medium-heuristic"),
        ],
    )
    async def test_single_observation_path_requires_very_high_and_judge_acceptance(
        self,
        store: ReviewStore,
        settings: ReviewSettings,
        heuristic: float,
        accepted: bool | None,
        judge_confidence: float,
        expected: bool,
    ) -> None:
        await store.enable(REPO)
        candidate_id = await fix_candidate(store)
        await seed(store, candidate_id, "k0", session="s0", occurred="2026-06-01T10:00:00+00:00", heuristic=heuristic)
        if accepted is not None:
            await judge(store, "k0", accepted=accepted, confidence=judge_confidence)
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert status.single_observation is expected
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is expected

    async def test_two_accepted_sessions_suffice_without_very_high(
        self, store: ReviewStore, settings: ReviewSettings
    ) -> None:
        await store.enable(REPO)
        candidate_id = await fix_candidate(store)
        for i in range(2):
            await seed(
                store, candidate_id, f"k{i}", session=f"s{i}", occurred=f"2026-06-01T1{i}:00:00+00:00", heuristic=MEDIUM
            )
            await judge(store, f"k{i}")
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.sessions, status.days, status.single_observation) == (2, 1, False)
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is True

    async def test_two_accepted_observations_in_one_session_are_not_enough(
        self, store: ReviewStore, settings: ReviewSettings
    ) -> None:
        await store.enable(REPO)
        candidate_id = await fix_candidate(store)
        for i in range(2):
            await seed(
                store, candidate_id, f"k{i}", session="s0", occurred=f"2026-06-01T1{i}:00:00+00:00", heuristic=MEDIUM
            )
            await judge(store, f"k{i}")
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.sessions, status.single_observation) == (1, False)
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is False

    async def test_cap_applies_to_fix_candidates(self, store: ReviewStore, settings: ReviewSettings) -> None:
        await store.enable(REPO)
        candidate_id = await fix_candidate(store)
        await seed(store, candidate_id, "k0", session="s0", occurred="2026-06-01T10:00:00+00:00", heuristic=VERY_HIGH)
        await judge(store, "k0")
        await open_pr(store, rule="other-a", opened_at=datetime.now(UTC))
        await open_pr(store, rule="other-b", opened_at=datetime.now(UTC))
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is False


class TestThresholdStatus:
    async def test_reports_partial_counts_for_explanation(self, store: ReviewStore, settings: ReviewSettings) -> None:
        await store.enable(REPO)
        candidate_id = await create_candidate(store)
        for i in range(2):
            await seed(store, candidate_id, f"k{i}", session=f"s{i}", occurred=f"2026-06-01T1{i}:00:00+00:00")
            await judge(store, f"k{i}")
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.sessions, settings.min_sessions) == (2, 3)
        assert (status.days, settings.min_days) == (1, 2)
        assert (status.open_prs, settings.max_open_prs) == (0, 2)

    async def test_unknown_candidate_raises(self, store: ReviewStore, settings: ReviewSettings) -> None:
        with pytest.raises(LookupError, match="no candidate with id 999"):
            await store.threshold_status(999, settings=settings, prompt_version=PROMPT_VERSION)


class TestRegroup:
    async def test_merge_reparents_shared_slug_and_sweeps_husks(self, store: ReviewStore) -> None:
        first, second = await seed_merge_pair(store)
        assert await store.regroup_create(prompt_version=PROMPT_VERSION) == (2, 0)
        [survivor] = await store.candidates()
        assert (survivor["rule"], survivor["status"], survivor["observations"]) == ("shared-slug", "watching", 2)
        assert survivor["sample_text"] == "text ka"
        assert survivor["id"] not in (first, second)
        for husk in (first, second):
            with pytest.raises(LookupError, match=f"no candidate with id {husk}"):
                await store.candidate(husk)

    async def test_second_regroup_is_idempotent_and_byte_identical(self, store: ReviewStore) -> None:
        await seed_merge_pair(store)
        assert await store.regroup_create(prompt_version=PROMPT_VERSION) == (2, 0)
        candidates, observations = (
            await dump_table(store, "candidates"),
            await dump_table(store, "candidate_observations"),
        )
        assert await store.regroup_create(prompt_version=PROMPT_VERSION) == (0, 0)
        assert await dump_table(store, "candidates") == candidates
        assert await dump_table(store, "candidate_observations") == observations

    @pytest.mark.parametrize(
        "terminal",
        [CandidateStatus.PR_OPEN, CandidateStatus.STALE, CandidateStatus.ACCEPTED, CandidateStatus.REJECTED],
        ids=lambda s: s.value,
    )
    async def test_terminal_candidates_are_immune(self, store: ReviewStore, terminal: CandidateStatus) -> None:
        candidate_id = await create_candidate(store, rule=digest_rule("term"))
        await seed(store, candidate_id, "kt", session="s1", occurred="2026-06-01T10:00:00+00:00")
        await judge(store, "kt", slug="different-slug")
        for step in PATHS[terminal]:
            await store.transition(candidate_id, step, pr_opened_at=datetime.now(UTC))
        assert await store.regroup_create(prompt_version=PROMPT_VERSION) == (0, 0)
        [row] = await store.candidates()
        assert (row["id"], row["rule"], row["status"], row["observations"]) == (
            candidate_id,
            digest_rule("term"),
            terminal.value,
            1,
        )

    async def test_fidelity_upgrade_moves_observation_never_duplicates(self, store: ReviewStore) -> None:
        digest = await create_candidate(store, rule=digest_rule("f"))
        await seed(store, digest, "kf", session="s1", occurred="2026-06-01T10:00:00+00:00")
        await judge(store, "kf", slug="slug-a", fidelity="summary")
        assert await store.regroup_create(prompt_version=PROMPT_VERSION) == (1, 0)
        [on_a] = await store.candidates()
        assert (on_a["rule"], on_a["observations"]) == ("slug-a", 1)
        await judge(store, "kf", slug="slug-b", fidelity="full")
        assert await store.regroup_create(prompt_version=PROMPT_VERSION) == (1, 0)
        [on_b] = await store.candidates()
        assert (on_b["rule"], on_b["observations"]) == ("slug-b", 1)
        assert len(await dump_table(store, "candidate_observations")) == 1

    async def test_fix_lane_and_unjudged_create_are_immune(self, store: ReviewStore) -> None:
        fix_id = await fix_candidate(store)
        await seed(store, fix_id, "kf", session="s1", occurred="2026-06-01T10:00:00+00:00")
        await judge(store, "kf", slug="would-be-slug")
        create_id = await create_candidate(store, rule=digest_rule("unj"))
        await seed(store, create_id, "kc", session="s2", occurred="2026-06-01T10:00:00+00:00")
        assert await store.regroup_create(prompt_version=PROMPT_VERSION) == (0, 0)
        rows = {int(str(r["id"])): r for r in await store.candidates()}
        assert set(rows) == {fix_id, create_id}
        assert (rows[fix_id]["candidate_kind"], rows[fix_id]["status"], rows[fix_id]["observations"]) == (
            "fix",
            "watching",
            1,
        )
        assert (rows[create_id]["rule"], rows[create_id]["status"], rows[create_id]["observations"]) == (
            digest_rule("unj"),
            "watching",
            1,
        )

    async def test_all_rejected_retires_while_mixed_stays_watching(self, store: ReviewStore) -> None:
        assert CandidateStatus.REJECTED in TRANSITIONS[CandidateStatus.WATCHING]
        rejected_id = await create_candidate(store, rule=digest_rule("rej"))
        for i, (session, day) in enumerate([("s1", "2026-06-01"), ("s2", "2026-06-02")]):
            await seed(store, rejected_id, f"r{i}", session=session, occurred=f"{day}T10:00:00+00:00")
            await judge(store, f"r{i}", accepted=False)
        mixed_id = await create_candidate(store, rule=digest_rule("mix"))
        await seed(store, mixed_id, "m0", session="s3", occurred="2026-06-01T10:00:00+00:00")
        await seed(store, mixed_id, "m1", session="s4", occurred="2026-06-02T10:00:00+00:00")
        await judge(store, "m0", accepted=False)
        assert await store.regroup_create(prompt_version=PROMPT_VERSION) == (0, 1)
        retired = await store.candidate(rejected_id)
        assert (retired["status"], retired["observations"], retired["sample_text"]) == ("rejected", 2, "text r0")
        assert (await candidate_row(store, mixed_id))["status"] == "watching"

    async def test_new_observation_attaches_to_terminal_and_stays_ineligible(
        self, store: ReviewStore, settings: ReviewSettings
    ) -> None:
        await store.enable(REPO)
        slug = "durable-rule"
        candidate_id = await create_candidate(store, rule=slug)
        for i, (session, day) in enumerate([("s1", "2026-06-01"), ("s2", "2026-06-01"), ("s3", "2026-06-02")]):
            await seed(store, candidate_id, f"e{i}", session=session, occurred=f"{day}T10:00:00+00:00")
            await judge(store, f"e{i}", slug=slug)
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is True
        await store.transition(candidate_id, CandidateStatus.PR_OPEN, pr_opened_at=datetime.now(UTC))
        await store.transition(candidate_id, CandidateStatus.ACCEPTED)
        assert await create_candidate(store, rule=slug) == candidate_id
        await seed(store, candidate_id, "fresh", session="s9", occurred="2026-06-05T10:00:00+00:00")
        await judge(store, "fresh", slug=slug)
        status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.status, status.sessions, status.days) == (CandidateStatus.ACCEPTED, 4, 3)
        assert (await candidate_row(store, candidate_id))["status"] == "accepted"
        assert await store.eligible(candidate_id, settings=settings, prompt_version=PROMPT_VERSION) is False
