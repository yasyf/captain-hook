"""Real headless Claude Code sessions inside the throwaway repo.

``run_session`` is one ``claude -p`` invocation; ``continue_session`` appends a
user turn to the most recent conversation in the cwd via ``-c -p``, which is how
authentic user-correction turns land in the transcript. Transcripts live at an
environment-specific path (``~/.claude/projects`` normally, ``~/.cc-pool`` under
a pool account), so rather than guess the directory, :func:`capture_hook_command`
wires a SessionEnd hook that records each session's ``transcript_path`` verbatim
from the hook payload into a sandbox file — also proving SessionEnd fires for
headless sessions.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from stress.sandbox import Sandbox

SESSION_TIMEOUT = 900


def claude_p(
    sandbox: Sandbox, *args: str, max_turns: int = 8, timeout: int = SESSION_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["claude", "-p", *args, "--model", "sonnet", "--permission-mode", "acceptEdits", "--max-turns", str(max_turns)],
        env=sandbox.env(),
        cwd=str(sandbox.repo),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_session(sandbox: Sandbox, prompt: str, **kwargs: int) -> subprocess.CompletedProcess[str]:
    return claude_p(sandbox, prompt, **kwargs)


def continue_session(sandbox: Sandbox, prompt: str, **kwargs: int) -> subprocess.CompletedProcess[str]:
    return claude_p(sandbox, "-c", prompt, **kwargs)


def capture_hook_command(capture_file: Path) -> str:
    return (
        'python3 -c "import sys,json; '
        f"open(r'{capture_file}','a').write(json.load(sys.stdin).get('transcript_path','')+chr(10))\""
    )


def captured_transcripts(capture_file: Path) -> list[Path]:
    from pathlib import Path as P

    if not capture_file.exists():
        return []
    seen: dict[str, Path] = {}
    for line in capture_file.read_text().splitlines():
        if line.strip() and P(line.strip()).is_file():
            seen[line.strip()] = P(line.strip())
    return sorted(seen.values(), key=lambda p: p.stat().st_mtime)
