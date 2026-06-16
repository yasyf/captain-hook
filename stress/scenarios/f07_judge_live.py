"""Live-judge scenarios: real Sonnet verdicts scored against frozen labels.

The golden gate replays the four captured hook-fire transcripts through scan +
judge and scores the verdicts with :func:`cc_transcript.judge.verdicts.golden_result`
(gate: >=12/14). The CREATE-label pass plants six corrections with hand labels
and allows one miss for model nondeterminism. Both unlink the sandbox's
``claude`` stub so the real CLI serves the judge, and pin
``HOOKS_REVIEW_MAX_OPEN_PRS=0`` so no candidate can go eligible and spawn a
brain at this tier.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from cc_transcript.judge.verdicts import GoldenRow, golden_result

from stress.corpus import correction_turns, write_transcript
from stress.db import query
from stress.drivers.proc import capt_hook
from stress.sandbox import Sandbox
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "hook_fires"
GOLDEN_GATE_MIN = 12
JUDGE_CONCURRENCY = 3
JUDGE_RETRIES = 3
JUDGE_ENV = {"HOOKS_REVIEW_JUDGE_TIER": "medium", "HOOKS_REVIEW_MAX_OPEN_PRS": "0"}
CREATE_LABELS: tuple[tuple[str, str, bool], ...] = (
    ("tooling", "never log with print in this repo, always use the loguru logger", True),
    ("workflow", "always run the full pytest suite before you claim a task is done", True),
    ("safety", "never force-push to main, ever, not even for trivial fixes", True),
    ("one-off", "no, rename just this one helper to parse_events", False),
    ("task-scoped", "for this migration only, keep both columns until the backfill lands", False),
    ("hedged", "maybe we should possibly use a dataclass here? up to you", False),
)
CREATE_LABELS_MAX_MISSES = 1


def load_golden() -> tuple[list[GoldenRow], str]:
    raw = (FIXTURES / "golden_review.json").read_bytes()
    rows = [
        GoldenRow(
            dedup_key=row["dedup_key"],
            source_kind=row["source_kind"],
            text=row["text"],
            expected=row["expected"] == "accepted",
            note=row["note"],
        )
        for row in json.loads(raw)
    ]
    return rows, hashlib.sha256(raw).hexdigest()


def go_live(sandbox: Sandbox) -> None:
    (sandbox.bin / "claude").unlink(missing_ok=True)
    capt_hook("review", "enable", sandbox=sandbox, cwd=sandbox.repo)


def spawn_over(sandbox: Sandbox, transcript: Path) -> str:
    proc = capt_hook(
        "review",
        "spawn",
        "--transcript",
        str(transcript),
        "--cwd",
        str(sandbox.repo),
        sandbox=sandbox,
        timeout=1200,
    )
    return proc.stdout + proc.stderr


async def judge_golden_texts(rows: list[GoldenRow]) -> list[tuple[bool, str]]:
    import asyncio
    import subprocess

    from captain_hook.review.judge import FIX_JUDGE_PROMPT, ReviewVerdict
    from cc_transcript.judge.llm import structured_judge

    judge = structured_judge(ReviewVerdict, tier="medium")
    semaphore = asyncio.Semaphore(JUDGE_CONCURRENCY)

    async def one(text: str) -> tuple[bool, str]:
        prompt = FIX_JUDGE_PROMPT.format(
            hook_name="(unknown hook)",
            event="(unknown)",
            action="(unknown)",
            fire_message="(not captured)",
            context="(not captured)",
            text=text,
        )
        for attempt in range(JUDGE_RETRIES):
            async with semaphore:
                try:
                    verdict = await judge(prompt)
                except subprocess.SubprocessError:
                    if attempt == JUDGE_RETRIES - 1:
                        raise
                    continue
            return verdict.accepted, verdict.category
        raise AssertionError("unreachable")

    return list(await asyncio.gather(*(one(row.text) for row in rows)))


def run_golden_gate(sandbox: Sandbox) -> ScenarioResult:
    import asyncio

    golden, sha = load_golden()
    judged = dict(zip([row.dedup_key for row in golden], asyncio.run(judge_golden_texts(golden)), strict=True))
    verdicts = {
        key: {"accepted": accepted, "category": category, "rationale": ""}
        for key, (accepted, category) in judged.items()
    }
    result = golden_result(golden, {row.dedup_key for row in golden}, verdicts, sha)
    return ScenarioResult(
        checks=(
            check("all 14 golden rows judged live", len(judged) == len(golden), f"judged={len(judged)}"),
            check(
                f"golden gate >= {GOLDEN_GATE_MIN}/14",
                result.passed >= GOLDEN_GATE_MIN,
                f"passed={result.passed}/{result.total} failures="
                + str([(f.dedup_key[:8], f.expected, verdicts[f.dedup_key]["category"]) for f in result.failures]),
            ),
        ),
        finding=(
            "golden_review.json dedup keys do not exist in a current-scan store (manifest fixtures yield "
            "one hook_complaint row per session x target under 2.0 keying, 14 golden keys never match) — "
            "the golden gate judges row texts directly; the frozen-key join is unusable for live scans"
        ),
    )


def run_create_labels(sandbox: Sandbox) -> ScenarioResult:
    go_live(sandbox)
    texts: dict[str, tuple[str, bool]] = {}
    for slug, text, expected in CREATE_LABELS:
        path = write_transcript(
            sandbox.transcripts / f"judge-{slug}.jsonl", correction_turns(text, session=f"stress-judge-{slug}")
        )
        spawn_over(sandbox, path)
        texts[text] = (slug, expected)
    rows = query(
        sandbox.review_db,
        "SELECT e.text, v.category, v.accepted FROM feedback_events e "
        "JOIN verdicts v ON v.dedup_key = e.dedup_key WHERE v.role = 'judge'",
    )
    by_text = {str(row["text"]): (str(row["category"]), bool(row["accepted"])) for row in rows}
    misses = [
        f"{slug}: expected accepted={expected} got {by_text.get(text)}"
        for text, (slug, expected) in texts.items()
        if text not in by_text or by_text[text][1] != expected
    ]
    return ScenarioResult(
        checks=(
            check("all six labels judged", len(by_text) >= len(CREATE_LABELS), str(sorted(by_text.values()))),
            check(
                f"<= {CREATE_LABELS_MAX_MISSES} label misses",
                len(misses) <= CREATE_LABELS_MAX_MISSES,
                "; ".join(misses) or "all labels matched",
            ),
        )
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario("judge-golden-gate", "judge", Tier.JUDGE, run_golden_gate, env_overrides=dict(JUDGE_ENV)),
        Scenario("judge-create-labels", "judge", Tier.JUDGE, run_create_labels, env_overrides=dict(JUDGE_ENV)),
    )
