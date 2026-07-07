"""F18 — CREATE shared-slug regroup: distinct correction texts that emit one shared slug collapse to a single candidate.

Proves the shared-emitted-slug regroup mechanics end to end through the real
``capt-hook`` CLI, not similarity inference: each planted correction carries the
same ``prefer-frozen-dataclasses`` slug in its ``[[judge:...]]`` marker, so the
offline judge stub emits one shared slug and the treadmill regroups all three
observations onto a single create candidate. The slug is dictated by the marker,
not read from the text — the live convergence probes cover real paraphrase
similarity. The scenario id stays ``create-paraphrase-grouping`` because the docs
and the migration plan reference it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from stress.corpus import durable_correction, write
from stress.db import query
from stress.drivers.proc import capt_hook, review_run, wait_for_report
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check, expect

if TYPE_CHECKING:
    from stress.sandbox import Sandbox

FAMILY = "create"
NO_BRAIN = {"HOOKS_REVIEW_MAX_OPEN_PRS": "0"}
SHARED_SLUG = "prefer-frozen-dataclasses"
# The marker dictates the slug the offline judge stub emits, so all three
# corrections share one emitted slug regardless of their text.
MARKER = f"[[judge:durable_style_rule:0.9:{SHARED_SLUG}]]"
PARAPHRASES: tuple[tuple[str, str, int], ...] = (
    (f"always use a frozen dataclass for config here, never a plain class {MARKER}", "stress-para-a", 1),
    (f"stop returning bare tuples from this endpoint, model it as a frozen dataclass {MARKER}", "stress-para-b", 1),
    (f"never store this state in a mutable dict, wrap it in an immutable dataclass {MARKER}", "stress-para-c", 2),
)


def review_cli(sandbox: Sandbox, *args: str) -> str:
    proc = capt_hook(
        "review",
        *args,
        sandbox=sandbox,
        cwd=sandbox.repo,
        env=sandbox.env(CLAUDE_PROJECT_DIR=str(sandbox.repo)),
    )
    return proc.stdout + proc.stderr


def run_paraphrase_grouping(sandbox: Sandbox) -> ScenarioResult:
    review_cli(sandbox, "enable")
    for nth, (text, session, day) in enumerate(PARAPHRASES, 1):
        review_run(
            sandbox, write(durable_correction(session, session=session, day=day, text=text), sandbox.transcripts)
        )
        wait_for_report(sandbox, count=nth)
    reports = wait_for_report(sandbox, count=len(PARAPHRASES))
    candidates = query(sandbox.review_db, "SELECT candidate_kind, rule, source_kind FROM candidates")
    observations = query(
        sandbox.review_db, "SELECT session_id, substr(occurred_at, 1, 10) AS day FROM candidate_observations"
    )
    listing = [line for line in review_cli(sandbox, "list").splitlines() if line.strip()]
    thresholds = review_cli(sandbox, "threshold-check")
    return ScenarioResult(
        (
            expect("each pass inserts one correction", [report.inserted for report in reports], [1, 1, 1]),
            expect("each pass judges its one new correction", [report.judged for report in reports], [1, 1, 1]),
            expect("three texts sharing one emitted slug collapse to one candidate", len(candidates), 1),
            check(
                "the surviving candidate is the shared-slug create rule",
                candidates[0] == {"candidate_kind": "create", "rule": SHARED_SLUG, "source_kind": "transcript_message"},
                candidates,
            ),
            check(
                "three observations across three sessions and two days",
                len(observations) == 3
                and {row["session_id"] for row in observations} == {session for _, session, _ in PARAPHRASES}
                and len({row["day"] for row in observations}) == 2,
                observations,
            ),
            check(
                "review list shows exactly one x3 create candidate", len(listing) == 1 and "x3:" in listing[0], listing
            ),
            check(
                "threshold-check reports sessions=3/3 days=2/2",
                "sessions=3/3" in thresholds and "days=2/2" in thresholds,
                thresholds,
            ),
            check("brain never spawned", not any(report.brain for report in reports), reports),
        )
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(
            "create-paraphrase-grouping",
            FAMILY,
            Tier.OFFLINE,
            run_paraphrase_grouping,
            env_overrides=dict(NO_BRAIN),
        ),
    )
