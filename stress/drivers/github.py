"""Throwaway GitHub repo lifecycle for the live brain leg.

Creates a private ``capt-hook-stress-<run>`` repo under the authed user, points
the sandbox repo's origin at it, registers the captain-hook plugin, and rewires
the wired SessionEnd command at the sandbox's ``capt-hook`` shim so the brain
runs from the local checkout (which loads the bundled skills via ``--plugin-dir``).
Teardown closes stray PRs and deletes the repo (``gh auth refresh -s delete_repo``
grants the needed scope).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook.cli import register_marketplace
from stress.sandbox import git

if TYPE_CHECKING:
    from pathlib import Path

    from stress.sandbox import Sandbox

GH_TIMEOUT = 60


@dataclass(frozen=True, slots=True)
class ThrowawayRepo:
    name: str
    url: str


def gh(*args: str, timeout: int = GH_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)


def gh_login() -> str:
    proc = gh("api", "user", "--jq", ".login")
    if proc.returncode != 0:
        raise RuntimeError(f"gh not authenticated: {proc.stderr.strip()}")
    return proc.stdout.strip()


def rewire_settings(sandbox: Sandbox) -> None:
    settings_path = sandbox.repo / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(exist_ok=True)
    command = f"{sandbox.bin}/capt-hook review run"
    group = {"hooks": [{"type": "command", "command": command}]}
    existing = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    settings_path.write_text(
        json.dumps(existing | {"hooks": (existing.get("hooks") or {}) | {"SessionEnd": [group]}}, indent=2)
    )


def seed_project_files(repo: Path) -> None:
    (repo / "app.py").write_text(
        'def main() -> None:\n    print("hello")\n\n\nif __name__ == "__main__":\n    main()\n'
    )
    (repo / ".gitignore").write_text(".claude/settings.local.json\n")


def create_throwaway(sandbox: Sandbox, *, run_id: str) -> ThrowawayRepo:
    login = gh_login()
    name = f"{login}/capt-hook-stress-{run_id}"
    created = gh("repo", "create", name, "--private", "--description", "capt-hook stress-test throwaway")
    if created.returncode != 0 and "already exists" not in created.stderr:
        raise RuntimeError(f"gh repo create failed: {created.stderr.strip()}")
    seed_project_files(sandbox.repo)
    register_marketplace(sandbox.repo)
    rewire_settings(sandbox)
    git(sandbox.repo, "remote", "set-url", "origin", f"https://github.com/{name}.git")
    git(sandbox.repo, "add", "-A")
    git(sandbox.repo, "commit", "-qm", "seed stress project")
    git(sandbox.repo, "push", "-qu", "origin", "main")
    return ThrowawayRepo(name=name, url=f"https://github.com/{name}")


def list_prs(repo: ThrowawayRepo) -> list[dict[str, object]]:
    proc = gh("pr", "list", "--repo", repo.name, "--state", "all", "--json", "url,headRefName,title,state,body")
    return json.loads(proc.stdout) if proc.returncode == 0 else []


def merge_pr(url: str) -> bool:
    return gh("pr", "merge", url, "--merge", "--delete-branch=false").returncode == 0


def close_pr(url: str) -> bool:
    return gh("pr", "close", url).returncode == 0


def delete_throwaway(repo: ThrowawayRepo) -> bool:
    for pr in list_prs(repo):
        if pr["state"] == "OPEN":
            close_pr(str(pr["url"]))
    return gh("repo", "delete", repo.name, "--yes").returncode == 0
