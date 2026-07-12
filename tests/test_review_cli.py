from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from cc_transcript.judge.similar import KeyOverlap
from cc_transcript.mining.sourcekind import TRANSCRIPT_MESSAGE
from click.testing import CliRunner

from captain_hook.cli import cli
from captain_hook.review.cli import STATUS_CHOICES
from captain_hook.review.repo import RepoKey
from captain_hook.review.settings import ReviewSettings
from captain_hook.review.store import CandidateKind, CandidateStatus, ReviewStore
from tests.helpers import run_cli
from tests.review_helpers import (
    CORRECTION,
    correction_entries,
    install_judge,
    requires_llm_backend,
    write_transcript,
)

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

GIT_REPO_KEY = RepoKey("github.com/yasyf/scratch")


def invoke(*args: str, root: Path) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), "review", *args])


def db_path() -> Path:
    return ReviewSettings().db_path


async def repo_watching(repo: RepoKey) -> bool:
    async with await ReviewStore.open(db_path()) as store:
        return await store.watching(repo)


async def candidate_status(candidate_id: int) -> tuple[str, object]:
    async with await ReviewStore.open(db_path()) as store:
        row = await store.candidate(candidate_id)
        return str(row["status"]), row["pr_opened_at"]


async def seed_pr_open(url: str) -> int:
    async with await ReviewStore.open(db_path()) as store:
        candidate_id = await store.ensure_candidate(
            GIT_REPO_KEY, kind=CandidateKind.CREATE, rule="r", source_kind=TRANSCRIPT_MESSAGE
        )
        await store.transition(candidate_id, CandidateStatus.PR_OPEN, pr_url=url)
        return candidate_id


@pytest.fixture
def scanned_repo(tmp_path: Path, git_repo: Path) -> Path:
    transcript = write_transcript(tmp_path / "s.jsonl", correction_entries(cwd=str(git_repo)))
    result = invoke("scan", "--transcript", str(transcript), root=git_repo)
    assert result.exit_code == 0, result.output
    return git_repo


class TestGroupSurface:
    def test_spawn_is_hidden_and_public_commands_listed(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["review", "--help"])
        assert result.exit_code == 0
        assert "spawn" not in result.output
        for command in (
            "run",
            "enable",
            "disable",
            "scan",
            "triage",
            "status",
            "list",
            "show",
            "threshold-check",
            "update",
            "sync-prs",
        ):
            assert command in result.output

    def test_status_choices_match_candidate_status(self) -> None:
        assert set(STATUS_CHOICES) == {status.value for status in CandidateStatus}


class TestEnableDisable:
    def test_enable_registers_plugin(self, git_repo: Path) -> None:
        assert invoke("enable", root=git_repo).exit_code == 0
        settings = json.loads((git_repo / ".claude" / "settings.json").read_text())
        assert settings["enabledPlugins"] == {"captain-hook@captain-hook": True}
        assert settings["extraKnownMarketplaces"]["captain-hook"]["source"] == {
            "source": "github",
            "repo": "yasyf/captain-hook",
        }

    def test_disable_unwatches(self, git_repo: Path) -> None:
        assert invoke("enable", root=git_repo).exit_code == 0
        result = invoke("disable", root=git_repo)
        assert result.exit_code == 0
        assert f"not watching {GIT_REPO_KEY}" in result.output
        assert asyncio.run(repo_watching(GIT_REPO_KEY)) is False

    def test_enable_outside_a_git_repo_fails(self, tmp_path: Path) -> None:
        result = invoke("enable", root=tmp_path)
        assert result.exit_code != 0
        assert "is not a git repo with an 'origin' remote" in result.output


class TestJudgeDefault:
    def test_judge_tier_ships_medium(self) -> None:
        assert ReviewSettings.model_fields["judge_tier"].default == "medium"


class TestInitEnablesReviewer:
    def test_init_watches_and_installs_for_git_repo(self, git_repo: Path) -> None:
        result = run_cli("init", root_dir=str(git_repo))
        assert result.returncode == 0, result.stderr
        assert asyncio.run(repo_watching(GIT_REPO_KEY)) is True
        assert f"watching {GIT_REPO_KEY}" in result.stdout
        settings = json.loads((git_repo / ".claude" / "settings.json").read_text())
        assert settings["enabledPlugins"] == {"captain-hook@captain-hook": True}

    def test_init_no_review_does_not_watch(self, git_repo: Path) -> None:
        result = run_cli("init", "--no-review", root_dir=str(git_repo))
        assert result.returncode == 0, result.stderr
        assert asyncio.run(repo_watching(GIT_REPO_KEY)) is False
        assert "--no-review" in result.stdout

    def test_init_outside_git_skips_review(self, tmp_path: Path) -> None:
        result = run_cli("init", root_dir=str(tmp_path))
        assert result.returncode == 0, result.stderr
        assert "needs a git repo" in result.stdout


class TestScanAndTriage:
    def test_scan_is_incremental(self, tmp_path: Path, git_repo: Path) -> None:
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries(cwd=str(git_repo)))
        first = invoke("scan", "--transcript", str(transcript), root=git_repo)
        assert first.exit_code == 0
        assert "scanned 1 transcripts, 1 new corrections" in first.output
        second = invoke("scan", "--transcript", str(transcript), root=git_repo)
        assert "scanned 0 transcripts, 0 new corrections" in second.output

    def test_scan_accepts_directories(self, tmp_path: Path, git_repo: Path) -> None:
        write_transcript(tmp_path / "proj" / "s.jsonl", correction_entries(cwd=str(git_repo)))
        result = invoke("scan", "--dir", str(tmp_path / "proj"), root=git_repo)
        assert result.exit_code == 0
        assert "scanned 1 transcripts, 1 new corrections" in result.output

    def test_scan_requires_a_path(self, git_repo: Path) -> None:
        assert invoke("scan", root=git_repo).exit_code != 0

    @requires_llm_backend
    def test_triage_judges_pending_rows(self, scanned_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = install_judge(monkeypatch)
        result = invoke("triage", root=scanned_repo)
        assert result.exit_code == 0, result.output
        assert "judged 1, failed 0, pending 0, merged 1, retired 0" in result.output
        assert len(calls) == 1
        again = invoke("triage", "--limit", "5", root=scanned_repo)
        assert "judged 0, failed 0, pending 0, merged 0, retired 0" in again.output

    @requires_llm_backend
    def test_triage_reports_possible_slug_splits(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_splits(store: object, *, prompt_version: int, threshold: float) -> list[KeyOverlap]:
            return [KeyOverlap("prefer-uv", "use-uv-not-pip", 0.93)]

        monkeypatch.setattr("cc_transcript.judge.similar.near_duplicate_keys", fake_splits)
        result = invoke("triage", root=git_repo)
        assert result.exit_code == 0, result.output
        assert "judged 0, failed 0, pending 0, merged 0, retired 0" in result.output
        assert "possible split: prefer-uv ~ use-uv-not-pip (0.93)" in result.output


class TestListShowThreshold:
    def test_list_shows_candidates_with_evidence(self, scanned_repo: Path) -> None:
        result = invoke("list", root=scanned_repo)
        assert result.exit_code == 0, result.output
        assert "#1 [watching] create/transcript_message x1:" in result.output
        assert CORRECTION[:40] in result.output

    def test_list_empty_repo(self, git_repo: Path) -> None:
        result = invoke("list", root=git_repo)
        assert result.exit_code == 0
        assert f"no candidates for {GIT_REPO_KEY}" in result.output

    def test_show_prints_row_and_thresholds(self, scanned_repo: Path) -> None:
        result = invoke("show", "1", root=scanned_repo)
        assert result.exit_code == 0, result.output
        assert "status: watching" in result.output
        assert "thresholds: sessions=0 days=0 open_prs=0 single_observation=False eligible=False" in result.output

    def test_show_unknown_candidate_fails(self, git_repo: Path) -> None:
        result = invoke("show", "99", root=git_repo)
        assert result.exit_code != 0
        assert "no candidate with id 99" in result.output

    def test_threshold_check_reports_eligibility(self, scanned_repo: Path) -> None:
        result = invoke("threshold-check", root=scanned_repo)
        assert result.exit_code == 0, result.output
        assert "#1 eligible=False sessions=0/3 days=0/2 open_prs=0/2 watching=False" in result.output


class TestUpdateAndSyncPrs:
    def test_update_moves_status_and_stamps_pr(self, scanned_repo: Path) -> None:
        url = "https://github.com/yasyf/scratch/pull/1"
        result = invoke("update", "1", "pr_open", "--pr-url", url, root=scanned_repo)
        assert result.exit_code == 0, result.output
        assert "#1 -> pr_open" in result.output
        status, opened_at = asyncio.run(candidate_status(1))
        assert status == "pr_open"
        assert opened_at is not None

    def test_update_rejects_invalid_transition(self, scanned_repo: Path) -> None:
        result = invoke("update", "1", "accepted", root=scanned_repo)
        assert result.exit_code != 0
        assert "watching -> accepted" in result.output

    def test_sync_prs_folds_gh_state(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "https://github.com/yasyf/scratch/pull/7"
        candidate_id = asyncio.run(seed_pr_open(url))
        monkeypatch.setattr("captain_hook.review.sync.gh_pr_state", lambda _url: "MERGED")
        result = invoke("sync-prs", root=git_repo)
        assert result.exit_code == 0, result.output
        assert "accepted 1, rejected 0, stale 0, unreachable 0" in result.output
        status, _ = asyncio.run(candidate_status(candidate_id))
        assert status == "accepted"
