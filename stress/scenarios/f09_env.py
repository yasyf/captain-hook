"""Environment-hazard scenarios: the no-cwd footgun, the uvx wiring, gh failures, and env sanitization."""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import TYPE_CHECKING

from stress.corpus import durable_correction, write
from stress.db import one
from stress.drivers.proc import capt_hook, session_end_payload, wait_for_report
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check

if TYPE_CHECKING:
    from stress.sandbox import Sandbox

FAMILY = "env"
REAL_CHECKOUT_KEY = "github.com/yasyf/captain-hook"
EXPECTED_WIRING = "uvx capt-hook review run"
PR_URL = "https://example.com/pr/1"
SENTINEL_VARS = ("HOOKS_REVIEW_MIN_SESSIONS", "HOOKS_STRESS_SENTINEL", "CAPT_HOOK_SPAWNED")


def cli_env(sandbox: Sandbox, **overrides: str) -> dict[str, str]:
    return {key: value for key, value in sandbox.env(**overrides).items() if key != "CLAUDE_PROJECT_DIR"}


def expected_repo(sandbox: Sandbox) -> str:
    return f"github.com/capt-hook-stress/{sandbox.root.name}"


def session_end_commands(sandbox: Sandbox) -> list[str]:
    hooks = json.loads((sandbox.repo / ".claude" / "settings.json").read_text()).get("hooks") or {}
    return [
        str(entry.get("command") or "") for group in hooks.get("SessionEnd") or [] for entry in group.get("hooks") or []
    ]


def no_cwd_footgun(sandbox: Sandbox) -> ScenarioResult:
    (scratch := sandbox.root / "scratch").mkdir()
    transcript = write(durable_correction("env-no-cwd"), sandbox.transcripts)
    payload = session_end_payload(transcript, cwd=None)
    proc = capt_hook("review", "run", sandbox=sandbox, stdin=payload, cwd=scratch)
    reports = wait_for_report(sandbox, count=1, timeout=120)
    report = reports[0] if reports else None
    return ScenarioResult(
        checks=(
            check("payload carries no cwd", '"cwd"' not in payload, payload),
            check(
                "spawn falls back to its own non-git cwd and bails",
                proc.returncode == 0
                and report is not None
                and report.repo in (None, expected_repo(sandbox))
                and not report.watching,
                f"rc={proc.returncode} report={report} scratch={scratch}",
            ),
            check(
                "resolved repo is not the real checkout",
                report is not None and report.repo != REAL_CHECKOUT_KEY,
                f"repo={report.repo if report else None!r} real={REAL_CHECKOUT_KEY!r}",
            ),
        )
    )


def uvx_wiring(sandbox: Sandbox) -> ScenarioResult:
    enable = capt_hook("review", "enable", sandbox=sandbox, cwd=sandbox.repo, env=cli_env(sandbox))
    commands = session_end_commands(sandbox)
    started = time.monotonic()
    help_proc = subprocess.run(
        ["uvx", "capt-hook", "--help"],
        env=sandbox.env(),
        cwd=str(sandbox.root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    elapsed = time.monotonic() - started
    return ScenarioResult(
        checks=(
            check(
                "enable wires the PyPI-resolving uvx command verbatim",
                enable.returncode == 0 and commands == [EXPECTED_WIRING],
                f"rc={enable.returncode} commands={commands} stdout={enable.stdout!r}",
            ),
            check(
                "sandbox uvx shim intercepts capt-hook fast",
                help_proc.returncode == 0 and elapsed < 10 and "Captain Hook" in help_proc.stdout,
                f"rc={help_proc.returncode} elapsed={elapsed:.2f}s stdout={help_proc.stdout[:200]!r}",
            ),
            check(
                "UV_EXCLUDE_NEWER scrubbed from sandbox env",
                "UV_EXCLUDE_NEWER" not in sandbox.env(),
                f"in_env={'UV_EXCLUDE_NEWER' in sandbox.env()} parent={os.environ.get('UV_EXCLUDE_NEWER')!r}",
            ),
        ),
        finding=(
            "review enable wires SessionEnd as 'uvx capt-hook review run': every real session end resolves"
            " capt-hook through uvx (PyPI wheel), never the local checkout, so live drivers must rewrite the"
            " wired command; the sandbox uvx shim intercepts it here"
        ),
    )


def gh_unreachable(sandbox: Sandbox) -> ScenarioResult:
    transcript = write(durable_correction("env-gh"), sandbox.transcripts, cwd=sandbox.repo)
    scan = capt_hook("review", "scan", "--transcript", str(transcript), sandbox=sandbox)
    seeded = one(sandbox.review_db, "SELECT id, repo_key, status FROM candidates")
    update = capt_hook("review", "update", str(seeded["id"]), "pr_open", "--pr-url", PR_URL, sandbox=sandbox)
    (config := sandbox.root / "gh-stub.json").write_text(json.dumps({PR_URL: "error"}))
    sync = capt_hook(
        "review",
        "sync-prs",
        "--repo",
        str(seeded["repo_key"]),
        sandbox=sandbox,
        cwd=sandbox.repo,
        env=cli_env(sandbox, STRESS_GH_STUB_CONFIG=str(config)),
    )
    after = one(sandbox.review_db, "SELECT id, status, pr_url FROM candidates WHERE id = ?", (seeded["id"],))
    return ScenarioResult(
        checks=(
            check(
                "scan seeds a candidate",
                scan.returncode == 0 and "1 new corrections" in scan.stdout,
                f"rc={scan.returncode} stdout={scan.stdout!r} row={seeded}",
            ),
            check(
                "update moves it to pr_open",
                update.returncode == 0 and f"#{seeded['id']} -> pr_open" in update.stdout,
                f"rc={update.returncode} stdout={update.stdout!r} stderr={update.stderr[:200]!r}",
            ),
            check(
                "sync-prs survives gh error and reports unreachable 1",
                sync.returncode == 0 and "unreachable 1" in sync.stdout,
                f"rc={sync.returncode} stdout={sync.stdout!r} stderr={sync.stderr[:300]!r}",
            ),
            check(
                "candidate stays pr_open",
                after == {"id": seeded["id"], "status": "pr_open", "pr_url": PR_URL},
                f"after={after}",
            ),
        )
    )


def env_sanitized(sandbox: Sandbox) -> ScenarioResult:
    saved = {key: os.environ.get(key) for key in SENTINEL_VARS}
    os.environ.update(dict.fromkeys(SENTINEL_VARS, "leaked"))
    try:
        env = sandbox.env()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    leaked = sorted(set(SENTINEL_VARS) & env.keys())
    hooks_keys = sorted(key for key in env if key.startswith("HOOKS_"))
    return ScenarioResult(
        checks=(
            check("HOOKS_* and CAPT_HOOK_SPAWNED sentinels dropped", not leaked, f"leaked={leaked}"),
            check("no HOOKS_-prefixed keys reach subprocess env", not hooks_keys, f"hooks_keys={hooks_keys}"),
            check(
                "state dir pinned inside sandbox",
                env["CAPTAIN_HOOK_STATE_DIR"].startswith(str(sandbox.root)),
                f"CAPTAIN_HOOK_STATE_DIR={env['CAPTAIN_HOOK_STATE_DIR']}",
            ),
        )
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(name="env-no-cwd-footgun", family=FAMILY, tier=Tier.OFFLINE, run=no_cwd_footgun),
        Scenario(name="env-uvx-wiring", family=FAMILY, tier=Tier.OFFLINE, run=uvx_wiring),
        Scenario(name="env-gh-unreachable", family=FAMILY, tier=Tier.OFFLINE, run=gh_unreachable),
        Scenario(name="env-sanitized", family=FAMILY, tier=Tier.OFFLINE, run=env_sanitized),
    )
