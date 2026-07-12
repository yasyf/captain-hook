from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from captain_hook.review.judge import JudgeReport, judge_pass
from captain_hook.review.repo import RepoKey
from captain_hook.review.scan import scan_transcript
from captain_hook.review.store import CandidateStatus, ReviewStore
from captain_hook.review.triage import TriageReport, triage_pass
from tests.review_helpers import (
    CORRECTION,
    assistant_text,
    correction_entries,
    install_fake_embedder,
    install_judge,
    install_resolved_model,
    install_triage,
    user_text,
    write_transcript,
)

if TYPE_CHECKING:
    from pathlib import Path

    from captain_hook.review.settings import ReviewSettings

REPO = RepoKey("github.com/yasyf/captain-hook")


async def scan_correction(store: ReviewStore, settings: ReviewSettings, tmp_path: Path, *, session: str = "s1") -> None:
    path = write_transcript(tmp_path / f"{session}.jsonl", correction_entries(session=session))
    await scan_transcript(store, path, settings=settings, repo_key=REPO)


async def statuses(store: ReviewStore) -> dict[str, str]:
    cur = await store.store.conn.execute("SELECT rule, status FROM candidates")
    return {str(row["rule"]): str(row["status"]) async for row in cur}


class TestTriagePass:
    async def test_junk_verdict_rejects_candidate_without_a_judge_call(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await scan_correction(store, settings, tmp_path)
        install_triage(monkeypatch, junk_when=lambda prompt: CORRECTION in prompt)
        judge_calls = install_judge(monkeypatch)
        install_resolved_model(monkeypatch)

        report = await triage_pass(store, settings=settings)
        assert report == TriageReport(triaged=1, junk=1, rejected=1)
        assert set((await statuses(store)).values()) == {CandidateStatus.REJECTED}

        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=0, failed=0, pending=0, merged=0, retired=0, reopened=0
        )
        assert judge_calls == []

    async def test_keep_on_doubt_leaves_the_candidate_for_the_judge(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await scan_correction(store, settings, tmp_path)
        install_triage(monkeypatch)
        install_fake_embedder(monkeypatch)
        judge_calls = install_judge(monkeypatch, category="durable_style_rule")
        install_resolved_model(monkeypatch)

        report = await triage_pass(store, settings=settings)
        assert report == TriageReport(triaged=1, junk=0, rejected=0)
        assert set((await statuses(store)).values()) == {CandidateStatus.WATCHING}

        assert (await judge_pass(store, settings=settings)).judged == 1
        assert len(judge_calls) == 1

    async def test_verdict_is_recorded_per_dedup_key_and_never_retriaged(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await scan_correction(store, settings, tmp_path)
        triage_calls = install_triage(monkeypatch, junk_when=lambda prompt: CORRECTION in prompt)

        first = await triage_pass(store, settings=settings)
        assert (first.triaged, len(triage_calls)) == (1, 1)

        second = await triage_pass(store, settings=settings)
        assert second == TriageReport(triaged=0, junk=0, rejected=0)
        assert len(triage_calls) == 1

    async def test_failed_triage_leaves_the_event_untriaged_to_retry(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await scan_correction(store, settings, tmp_path)
        install_triage(monkeypatch, fail_on=CORRECTION)

        assert await triage_pass(store, settings=settings) == TriageReport(triaged=0, junk=0, rejected=0)
        assert await store.junk_triaged_keys() == set()
        assert len(await store.untriaged_create_events(limit=10)) == 1

    async def test_a_judge_verdict_blocks_a_later_junk_retry(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await scan_correction(store, settings, tmp_path)
        install_triage(monkeypatch, fail_on=CORRECTION)
        assert (await triage_pass(store, settings=settings)).triaged == 0

        install_fake_embedder(monkeypatch)
        install_judge(monkeypatch, category="durable_style_rule")
        install_resolved_model(monkeypatch)
        assert (await judge_pass(store, settings=settings)).judged == 1

        install_triage(monkeypatch, junk_when=lambda prompt: True)
        assert await triage_pass(store, settings=settings) == TriageReport(triaged=0, junk=0, rejected=0)
        assert await store.junk_triaged_keys() == set()
        assert CandidateStatus.REJECTED not in set((await statuses(store)).values())

    async def test_mixed_evidence_keeps_the_candidate_watching(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        junk_path = write_transcript(
            tmp_path / "junk.jsonl", [assistant_text("done"), user_text("this junk message here")]
        )
        await scan_transcript(store, junk_path, settings=settings, repo_key=REPO)
        await scan_correction(store, settings, tmp_path, session="s2")
        install_triage(monkeypatch, junk_when=lambda prompt: "junk message" in prompt)

        report = await triage_pass(store, settings=settings)
        assert (report.triaged, report.junk, report.rejected) == (2, 1, 1)
        statuses_by_rule = await statuses(store)
        assert sorted(statuses_by_rule.values()) == sorted([CandidateStatus.REJECTED, CandidateStatus.WATCHING])

    async def test_cap_bounds_calls_and_the_rest_retries_next_pass(
        self, store: ReviewStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from captain_hook.review.settings import ReviewSettings

        settings = ReviewSettings(db_path=tmp_path / "review.db", max_triage_calls_per_session=1)
        for i in range(3):
            path = write_transcript(
                tmp_path / f"c{i}.jsonl", [assistant_text("x"), user_text(f"distinct correction number {i} here")]
            )
            await scan_transcript(store, path, settings=settings, repo_key=REPO)
        calls = install_triage(monkeypatch)

        assert (await triage_pass(store, settings=settings)).triaged == 1
        assert len(calls) == 1
        assert (await triage_pass(store, settings=settings)).triaged == 1
        assert len(await store.untriaged_create_events(limit=10)) == 1
