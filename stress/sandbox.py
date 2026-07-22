"""Per-scenario isolation: a throwaway state root, repo, and PATH of shims.

Everything the pipeline persists — review.db, spawn.log, the decision ledger —
lands under the sandbox via ``CAPTAIN_HOOK_STATE_DIR`` and
``CAPT_HOOK_DECISIONS_DB``. The real-state guard cannot hash-compare the user's
live state (their own concurrent sessions append to it constantly); instead
every sandbox artifact carries a recognizable fingerprint — ``/tmp/capt-stress``
paths, ``stress-`` session ids, ``capt-hook-stress`` repo keys — and the guard
queries the real stores for those fingerprints after the run. Any hit is
harness leakage and fails the whole run regardless of scenario outcomes.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from stress.shims import install_base_shims, install_codex_stub, install_gh_stub, install_judge_stub

RUN_ROOT = Path("/tmp/capt-stress")
CHECKOUT = Path(__file__).resolve().parents[1]
DROPPED_ENV = (
    "CAPT_HOOK_SPAWNED",
    "UV_EXCLUDE_NEWER",
    "CAPTAIN_HOOK_STATE_DIR",
    "CAPT_HOOK_DECISIONS_DB",
    "CLAUDE_PROJECT_DIR",
    "CAPT_HOOK_CLIENT_TIMEOUT",
)
GIT_IDENTITY = ("-c", "user.email=stress@capt-hook.test", "-c", "user.name=capt-hook-stress")
SESSION_PREFIX = "stress-"
ORIGIN_ORG = "capt-hook-stress"
REAL_REVIEW_DB = Path.home() / ".claude" / "state" / "review" / "review.db"
REAL_DECISIONS_DB = Path.home() / ".cc-transcript" / "decisions.db"
REAL_SPAWN_LOG = Path.home() / ".claude" / "state" / "review" / "spawn.log"
LEAK_QUERIES = (
    (REAL_REVIEW_DB, f"SELECT COUNT(*) FROM candidates WHERE repo_key LIKE '%{ORIGIN_ORG}%'"),
    (REAL_REVIEW_DB, f"SELECT COUNT(*) FROM repos WHERE repo_key LIKE '%{ORIGIN_ORG}%'"),
    (REAL_REVIEW_DB, "SELECT COUNT(*) FROM files WHERE path LIKE '/tmp/capt-stress%'"),
    (REAL_DECISIONS_DB, f"SELECT COUNT(*) FROM decisions WHERE session_id LIKE '{SESSION_PREFIX}%'"),
)


@dataclass(frozen=True, slots=True)
class Sandbox:
    root: Path
    env_overrides: dict[str, str] = field(default_factory=dict)

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def transcripts(self) -> Path:
        return self.root / "transcripts"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def review_db(self) -> Path:
        return self.state_dir / "review" / "review.db"

    @property
    def spawn_log(self) -> Path:
        return self.state_dir / "review" / "spawn.log"

    @property
    def decisions_db(self) -> Path:
        return self.state_dir / "decisions.db"

    def pr_url(self, n: int) -> str:
        return f"https://github.com/{ORIGIN_ORG}/{self.root.name}/pull/{n}"

    def env(self, **overrides: str) -> dict[str, str]:
        base = {
            key: value for key, value in os.environ.items() if key not in DROPPED_ENV and not key.startswith("HOOKS_")
        }
        return (
            base
            | {
                "CAPTAIN_HOOK_STATE_DIR": str(self.state_dir),
                "CAPT_HOOK_DECISIONS_DB": str(self.decisions_db),
                "CAPTAIN_HOOK_LOG_DIR": str(self.state_dir / "logs"),
                "PATH": f"{self.bin}:{os.environ['PATH']}",
            }
            | self.env_overrides
            | overrides
        )

    def spawn_log_text(self) -> str:
        return self.spawn_log.read_text() if self.spawn_log.exists() else ""

    def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *GIT_IDENTITY, *args], check=True, capture_output=True)


def init_repo(repo: Path, *, origin: str) -> None:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", origin)
    (repo / "README.md").write_text("# stress sandbox\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")


def create_sandbox(
    run_dir: Path,
    name: str,
    *,
    env_overrides: dict[str, str] | None = None,
    judge_stub: bool = True,
    codex_stub: bool = True,
    gh_stub: bool = True,
) -> Sandbox:
    root = run_dir / name
    if root.exists():
        shutil.rmtree(root)
    sandbox = Sandbox(root, env_overrides=env_overrides or {})
    for path in (sandbox.state_dir / "review", sandbox.transcripts, sandbox.bin):
        path.mkdir(parents=True, exist_ok=True)
    init_repo(sandbox.repo, origin=f"git@github.com:capt-hook-stress/{name}.git")
    install_base_shims(sandbox.bin)
    if judge_stub:
        install_judge_stub(sandbox.bin)
    if codex_stub:
        install_codex_stub(sandbox.bin)
    if gh_stub:
        install_gh_stub(sandbox.bin)
    return sandbox


def leak_count(db: Path, query: str) -> int:
    if not db.exists():
        return 0
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        try:
            return int(conn.execute(query).fetchone()[0])
        except sqlite3.OperationalError:
            return 0


def real_state_leaks() -> list[str]:
    counts = [f"{db.name}: {query} -> {n}" for db, query in LEAK_QUERIES if (n := leak_count(db, query))]
    spawn_log = REAL_SPAWN_LOG.read_text() if REAL_SPAWN_LOG.exists() else ""
    return (
        counts
        + (["real spawn.log mentions /tmp/capt-stress"] if "/tmp/capt-stress" in spawn_log else [])
    )
