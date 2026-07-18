"""F8 — the eligibility truth table: session/day floors, verdict recency, PR caps, and transitions."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from captain_hook.review.store import ReviewStore
from stress import corpus
from stress.db import one, query
from stress.drivers.proc import capt_hook
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check, expect
from stress.seeds import inject_verdict, seed_decision

if TYPE_CHECKING:
    from pathlib import Path
    from subprocess import CompletedProcess

    from captain_hook.review.judge import Category
    from stress.sandbox import Sandbox

FAMILY = "thresholds"
TEXT_A = corpus.DURABLE_CORRECTION_LIVE
TEXT_B = "never push directly to main, open a pull request instead"
TEXT_C = "always run the linter before committing anything"
PR_URL = "https://github.com/capt-hook-stress/thresholds/pull/{n}"
FIX_SESSION = "stress-fix-strong"
DECISION_TS = "2026-06-01T12:01:00+00:00"


def review_cli(sandbox: Sandbox, *args: str, **env_overrides: str) -> CompletedProcess[str]:
    return capt_hook(
        "review",
        *args,
        sandbox=sandbox,
        cwd=sandbox.repo,
        env=sandbox.env(CLAUDE_PROJECT_DIR=str(sandbox.repo), **env_overrides),
    )


def enable(sandbox: Sandbox) -> None:
    proc = review_cli(sandbox, "enable")
    assert proc.returncode == 0, proc.stderr


def scan_transcripts(sandbox: Sandbox, *paths: Path) -> CompletedProcess[str]:
    return review_cli(sandbox, "scan", *[arg for path in paths for arg in ("--transcript", str(path))])


def write_session(sandbox: Sandbox, name: str, *, session: str, day: int, text: str = TEXT_A) -> Path:
    planted = corpus.durable_correction(name, session=session, day=day, text=text)
    return corpus.write(planted, sandbox.transcripts, cwd=sandbox.repo)


def write_sessions(sandbox: Sandbox, slug: str, *, days: tuple[int, ...], text: str = TEXT_A) -> list[Path]:
    return [
        write_session(sandbox, f"{slug}-{n}", session=f"stress-{slug}-{n}", day=day, text=text)
        for n, day in enumerate(days, 1)
    ]


def inject_all(
    sandbox: Sandbox,
    *,
    category: Category,
    confidence: float = 0.9,
    model: str = "stress-injected",
    fidelity: str = "full",
) -> list[str]:
    keys = [str(row["dedup_key"]) for row in query(sandbox.review_db, "SELECT dedup_key FROM feedback_events")]

    async def go() -> None:
        with ReviewStore.open(sandbox.review_db) as store:
            for key in keys:
                await inject_verdict(
                    store, key, category=category, confidence=confidence, model=model, fidelity=fidelity
                )

    asyncio.run(go())
    return keys


def candidate_id(sandbox: Sandbox, text: str) -> int:
    row = one(
        sandbox.review_db,
        "SELECT o.candidate_id AS id FROM candidate_observations o "
        "JOIN feedback_events e ON e.dedup_key = o.dedup_key WHERE e.text = ? LIMIT 1",
        (text,),
    )
    return int(row["id"])


def threshold_line(sandbox: Sandbox, cid: int, **env_overrides: str) -> str:
    return review_cli(sandbox, "threshold-check", str(cid), **env_overrides).stdout.strip()


def create_eligible_exact_boundary(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    scanned = scan_transcripts(sandbox, *write_sessions(sandbox, "boundary", days=(1, 1, 2)))
    inject_all(sandbox, category="tooling_rule")
    cid = candidate_id(sandbox, TEXT_A)
    return ScenarioResult(
        (
            expect("scan stdout", scanned.stdout.strip(), "scanned 3 transcripts, 3 new corrections"),
            expect(
                "threshold-check at the exact boundary",
                threshold_line(sandbox, cid),
                f"#{cid} eligible=True sessions=3/3 days=2/2 open_prs=0/2 watching=True",
            ),
        )
    )


def create_below_sessions(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    scanned = scan_transcripts(sandbox, *write_sessions(sandbox, "two-sess", days=(1, 2)))
    inject_all(sandbox, category="tooling_rule")
    cid = candidate_id(sandbox, TEXT_A)
    return ScenarioResult(
        (
            expect("scan stdout", scanned.stdout.strip(), "scanned 2 transcripts, 2 new corrections"),
            expect(
                "threshold-check below the session floor",
                threshold_line(sandbox, cid),
                f"#{cid} eligible=False sessions=2/3 days=2/2 open_prs=0/2 watching=True",
            ),
        )
    )


def create_below_days(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    scanned = scan_transcripts(sandbox, *write_sessions(sandbox, "one-day", days=(1, 1, 1)))
    inject_all(sandbox, category="tooling_rule")
    cid = candidate_id(sandbox, TEXT_A)
    return ScenarioResult(
        (
            expect("scan stdout", scanned.stdout.strip(), "scanned 3 transcripts, 3 new corrections"),
            expect(
                "threshold-check below the day floor",
                threshold_line(sandbox, cid),
                f"#{cid} eligible=False sessions=3/3 days=1/2 open_prs=0/2 watching=True",
            ),
        )
    )


def confidence_floor(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    scan_transcripts(sandbox, *write_sessions(sandbox, "low-conf", days=(1, 1, 2)))
    keys = inject_all(sandbox, category="tooling_rule", confidence=0.55)
    cid = candidate_id(sandbox, TEXT_A)
    return ScenarioResult(
        (
            expect("injected verdicts", len(keys), 3),
            expect(
                "accepted verdicts below min_judge_confidence count nothing",
                threshold_line(sandbox, cid),
                f"#{cid} eligible=False sessions=0/3 days=0/2 open_prs=0/2 watching=True",
            ),
        )
    )


def rejected_verdict_overrides(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    scan_transcripts(sandbox, *write_sessions(sandbox, "flip", days=(1, 1, 2)))
    inject_all(sandbox, category="tooling_rule", fidelity="summary")
    cid = candidate_id(sandbox, TEXT_A)
    accepted_line = threshold_line(sandbox, cid)
    inject_all(sandbox, category="one_off_correction", model="stress-injected-2", fidelity="full")
    return ScenarioResult(
        (
            expect(
                "a summary-fidelity accepted verdict makes the candidate eligible",
                accepted_line,
                f"#{cid} eligible=True sessions=3/3 days=2/2 open_prs=0/2 watching=True",
            ),
            expect(
                "a full-fidelity rejected verdict upgrades over the summary accept and revokes eligibility",
                threshold_line(sandbox, cid),
                f"#{cid} eligible=False sessions=0/3 days=0/2 open_prs=0/2 watching=True",
            ),
        )
    )


def open_pr_cap(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    eligible_paths = write_sessions(sandbox, "cap-a", days=(1, 1, 2))
    blockers = [
        write_session(sandbox, f"cap-{slug}", session=f"stress-cap-{slug}", day=1, text=text)
        for slug, text in (("b", TEXT_B), ("c", TEXT_C))
    ]
    scan_transcripts(sandbox, *eligible_paths, *blockers)
    inject_all(sandbox, category="tooling_rule")
    cid = candidate_id(sandbox, TEXT_A)
    moved = [
        review_cli(sandbox, "update", str(candidate_id(sandbox, text)), "pr_open", "--pr-url", PR_URL.format(n=n))
        for n, text in enumerate((TEXT_B, TEXT_C), 1)
    ]
    return ScenarioResult(
        (
            expect("two blocker candidates moved to pr_open", [proc.returncode for proc in moved], [0, 0]),
            expect(
                "cap reached: 2 fresh PRs block eligibility",
                threshold_line(sandbox, cid),
                f"#{cid} eligible=False sessions=3/3 days=2/2 open_prs=2/2 watching=True",
            ),
            expect(
                "HOOKS_REVIEW_MAX_OPEN_PRS=3 lifts the cap",
                threshold_line(sandbox, cid, HOOKS_REVIEW_MAX_OPEN_PRS="3"),
                f"#{cid} eligible=True sessions=3/3 days=2/2 open_prs=2/3 watching=True",
            ),
        )
    )


def transitions_enforced(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    scan_transcripts(sandbox, write_session(sandbox, "transitions", session="stress-tr-1", day=1))
    cid = str(candidate_id(sandbox, TEXT_A))
    skip = review_cli(sandbox, "update", cid, "accepted")
    opened = review_cli(sandbox, "update", cid, "pr_open", "--pr-url", PR_URL.format(n=1))
    accepted = review_cli(sandbox, "update", cid, "accepted")
    terminal = review_cli(sandbox, "update", cid, "rejected")
    unknown = review_cli(sandbox, "update", "9999", "pr_open", "--pr-url", PR_URL.format(n=9))
    return ScenarioResult(
        (
            check(
                "watching -> accepted refused",
                skip.returncode != 0 and "watching -> accepted" in skip.stderr,
                f"rc={skip.returncode} stderr={skip.stderr.strip()}",
            ),
            expect("watching -> pr_open ok", (opened.returncode, opened.stdout.strip()), (0, f"#{cid} -> pr_open")),
            expect(
                "pr_open -> accepted ok", (accepted.returncode, accepted.stdout.strip()), (0, f"#{cid} -> accepted")
            ),
            check(
                "accepted -> rejected refused (terminal)",
                terminal.returncode != 0 and "accepted -> rejected" in terminal.stderr,
                f"rc={terminal.returncode} stderr={terminal.stderr.strip()}",
            ),
            check(
                "unknown id refused",
                unknown.returncode != 0 and "no candidate" in unknown.stderr,
                f"rc={unknown.returncode} stderr={unknown.stderr.strip()}",
            ),
        )
    )


def fix_single_observation(sandbox: Sandbox) -> ScenarioResult:
    seed_decision(
        sandbox.decisions_db,
        ts_ms=int(datetime.fromisoformat(DECISION_TS).timestamp() * 1000),
        session_id=FIX_SESSION,
    )
    enable(sandbox)
    scanned = scan_transcripts(sandbox, corpus.write(corpus.fix_strong(), sandbox.transcripts, cwd=sandbox.repo))
    inject_all(sandbox, category="misfire_confirmed")
    row = one(
        sandbox.review_db,
        "SELECT id, candidate_kind, target_source_file, target_hook_name, misfire_class FROM candidates",
    )
    shown = review_cli(sandbox, "show", str(row["id"]))
    return ScenarioResult(
        (
            expect("scan stdout", scanned.stdout.strip(), "scanned 1 transcripts, 1 new corrections"),
            expect(
                "fix candidate attribution",
                (row["candidate_kind"], row["target_source_file"], row["target_hook_name"], row["misfire_class"]),
                ("fix", ".claude/hooks/status_nudge.py", "status_nudge:nudge_c424798f", "refire"),
            ),
            expect(
                "show thresholds line",
                shown.stdout.strip().splitlines()[-1],
                "thresholds: sessions=1 days=1 open_prs=0 single_observation=True eligible=True",
            ),
        )
    )


def scenarios() -> tuple[Scenario, ...]:
    return tuple(
        Scenario(name=name, family=FAMILY, tier=Tier.OFFLINE, run=run)
        for name, run in (
            ("create-eligible-exact-boundary", create_eligible_exact_boundary),
            ("create-below-sessions", create_below_sessions),
            ("create-below-days", create_below_days),
            ("confidence-floor", confidence_floor),
            ("rejected-verdict-overrides", rejected_verdict_overrides),
            ("open-pr-cap", open_pr_cap),
            ("transitions-enforced", transitions_enforced),
            ("fix-single-observation", fix_single_observation),
        )
    )
