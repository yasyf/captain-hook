"""The live end-to-end leg: real Claude sessions, real SessionEnds, a real brain, real PRs.

Two brain-tier scenarios:

``e2e-sessionend-autofire`` proves the production path — a real headless
``claude -p`` session whose SessionEnd fires the wired ``capt-hook review run``,
which detaches ``review spawn`` and lands a ``SpawnReport`` in the sandbox
spawn.log. Cheap: no GitHub, no brain (``max_open_prs=0``).

``e2e-live-brain`` drives the full arc inside a throwaway private GitHub repo.
Two headless sessions each get corrected with the same verbatim rule; their real
transcripts (captured from the SessionEnd payload, env-agnostic) are fed to
``review spawn`` synchronously, so the eligible pass blocks on the real brain
until it opens a hook PR. Then ``sync-prs`` folds a merge back to ``accepted``.
The throwaway repo is left for inspection; clean up with
``python -m stress.cli nuke-github --i-know``.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from stress.db import count, query
from stress.drivers.claude_live import capture_hook_command, captured_transcripts, continue_session, run_session
from stress.drivers.github import create_throwaway, gh, list_prs, merge_pr
from stress.drivers.proc import capt_hook, wait_for_report
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check

if TYPE_CHECKING:
    from stress.sandbox import Sandbox

CORRECTION = "never use print for status output in this repo, always use the loguru logger"
SESSION_PROMPTS = (
    "Read app.py, then add a --count flag whose handler emits a status line with print() per greeting. Keep it small.",
    "Read app.py, then add a small helper that emits a done message with print() after main() runs. Keep it small.",
)
AUTOFIRE_ENV = {"HOOKS_REVIEW_MAX_OPEN_PRS": "0"}
BRAIN_ENV = {
    "HOOKS_REVIEW_JUDGE_TIER": "medium",
    "HOOKS_REVIEW_MIN_SESSIONS": "2",
    "HOOKS_REVIEW_MIN_DAYS": "1",
    "HOOKS_REVIEW_BRAIN_MAX_BUDGET_USD": "3.0",
    "HOOKS_REVIEW_BRAIN_MAX_TURNS": "40",
}
SPAWN_TIMEOUT = 1800
AUTOFIRE_WAIT = 180


def wire_session_end(sandbox: Sandbox, command: str) -> None:
    path = sandbox.repo / ".claude" / "settings.json"
    path.parent.mkdir(exist_ok=True)
    existing = json.loads(path.read_text()) if path.exists() else {}
    group = {"hooks": [{"type": "command", "command": command}]}
    path.write_text(json.dumps(existing | {"hooks": (existing.get("hooks") or {}) | {"SessionEnd": [group]}}, indent=2))


def run_autofire(sandbox: Sandbox) -> ScenarioResult:
    (sandbox.bin / "claude").unlink(missing_ok=True)
    capt_hook("review", "enable", sandbox=sandbox, cwd=sandbox.repo)
    wire_session_end(sandbox, f"{sandbox.bin}/capt-hook review run")
    (sandbox.repo / "app.py").write_text("x = 1\n")
    session = run_session(sandbox, "Reply with the single word: ok", max_turns=1, timeout=150)
    reports = wait_for_report(sandbox, count=1, timeout=AUTOFIRE_WAIT)
    log = sandbox.spawn_log_text()
    return ScenarioResult(
        checks=(
            check("headless session ran", session.returncode == 0, f"rc={session.returncode}"),
            check("SessionEnd auto-fired the reviewer", bool(reports), log[-200:] or "spawn.log empty"),
            check("reviewer resolved the sandbox repo, watching", any(r.watching for r in reports), str(reports)),
            check(
                "no traceback in the detached child",
                log.count("Traceback") == 0,
                f"tracebacks={log.count('Traceback')}",
            ),
        )
    )


def spawn_over(sandbox: Sandbox, transcript: str) -> str:
    proc = capt_hook(
        "review",
        "spawn",
        "--transcript",
        transcript,
        "--cwd",
        str(sandbox.repo),
        sandbox=sandbox,
        timeout=SPAWN_TIMEOUT,
    )
    return proc.stdout + proc.stderr


def run_e2e(sandbox: Sandbox) -> ScenarioResult:
    (sandbox.bin / "claude").unlink(missing_ok=True)
    (sandbox.bin / "gh").unlink(missing_ok=True)
    repo = create_throwaway(sandbox, run_id=datetime.now(UTC).strftime("%Y%m%d%H%M%S"))
    capt_hook("review", "enable", sandbox=sandbox, cwd=sandbox.repo)
    capture_file = sandbox.root / "transcripts.txt"
    wire_session_end(sandbox, capture_hook_command(capture_file))
    started = time.monotonic()

    session_rcs = [
        (run_session(sandbox, prompt).returncode, continue_session(sandbox, CORRECTION).returncode)
        for prompt in SESSION_PROMPTS
    ]

    transcripts = captured_transcripts(capture_file)
    spawn_outputs = [spawn_over(sandbox, str(path)) for path in transcripts]
    elapsed = time.monotonic() - started

    candidates = query(sandbox.review_db, "SELECT id, status, pr_url, candidate_kind FROM candidates ORDER BY id")
    distinct_sessions = count(sandbox.review_db, "SELECT COUNT(DISTINCT session_id) FROM candidate_observations")
    accepted_verdicts = count(sandbox.review_db, "SELECT COUNT(*) FROM verdicts WHERE accepted = 1")
    prs = list_prs(repo)

    pr_checks: list[tuple[str, bool, str]] = []
    if prs:
        url = str(prs[0]["url"])
        diff = gh("pr", "diff", url)
        pr_checks = [
            (
                "PR branch is capt-hook/review/*",
                str(prs[0]["headRefName"]).startswith("capt-hook/review/"),
                str(prs[0]["headRefName"]),
            ),
            ("PR adds a hook under .claude/hooks/", ".claude/hooks/" in diff.stdout, diff.stdout[:200]),
            (
                "PR body cites the correction",
                "loguru" in str(prs[0]["body"]).lower() or "print" in str(prs[0]["body"]).lower(),
                str(prs[0]["body"])[:200],
            ),
        ]
        if merge_pr(url):
            time.sleep(5)
            sync = capt_hook("review", "sync-prs", sandbox=sandbox, cwd=sandbox.repo)
            pr_checks.append(("sync-prs folded merge to accepted", "accepted 1" in sync.stdout, sync.stdout.strip()))

    return ScenarioResult(
        checks=(
            check(
                "both live sessions + corrections ran", all(a == 0 and b == 0 for a, b in session_rcs), str(session_rcs)
            ),
            check(
                ">=2 real transcripts captured from SessionEnd payloads",
                len(transcripts) >= 2,
                str([t.name for t in transcripts]),
            ),
            check(
                ">=2 distinct sessions observed on a candidate",
                distinct_sessions >= 2,
                f"distinct sessions={distinct_sessions} candidates={candidates}",
            ),
            check(
                "live judge accepted the correction", accepted_verdicts >= 1, f"accepted verdicts={accepted_verdicts}"
            ),
            check(
                "a brain run was triggered",
                any("brain=True" in out for out in spawn_outputs),
                "; ".join(o[-120:] for o in spawn_outputs),
            ),
            check(
                "a real PR was opened by the brain",
                bool(prs),
                f"prs={[(p['url'], p['state']) for p in prs]} elapsed={elapsed:.0f}s",
            ),
            *(check(name, ok, ev) for name, ok, ev in pr_checks),
        ),
        finding=f"throwaway repo {repo.url} kept for inspection; time-to-PR ~{elapsed:.0f}s",
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario("e2e-sessionend-autofire", "e2e", Tier.BRAIN, run_autofire, env_overrides=dict(AUTOFIRE_ENV)),
        Scenario("e2e-live-brain", "e2e", Tier.BRAIN, run_e2e, env_overrides=dict(BRAIN_ENV)),
    )
