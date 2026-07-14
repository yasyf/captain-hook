"""Rewrite `pip install` to `uv pip install`, one occurrence at a time."""

from __future__ import annotations

import shlex

from captain_hook import (
    Allow,
    BaseHookEvent,
    Input,
    Occurrence,
    Rewrite,
    rewrite_command_occurrences,
)


def pip_to_uv(evt: BaseHookEvent, occ: Occurrence) -> str | None:
    cmd = occ.command
    if occ.piped or cmd.redirects or cmd.env:
        return None  # only rewrite what the splice can reproduce in full
    if cmd.executable != "pip" or not cmd.args or cmd.args[0] != "install":
        return None
    return shlex.join(["uv", "pip", *cmd.args])


rewrite_command_occurrences(
    to=pip_to_uv,
    note=lambda evt, pairs: f"Rewrote {len(pairs)} pip install(s) to uv pip: same resolver, faster installs.",
    tests={
        Input(command="pip install requests"): Rewrite(pattern="uv pip install requests"),
        # Only the pip segment is rewritten; the cd and pytest survive byte-for-byte.
        Input(command="cd api && pip install -r requirements.txt && pytest"): Rewrite(
            pattern="cd api && uv pip install -r requirements.txt && pytest"
        ),
        Input(command="pip install 'foo>=2'"): Rewrite(pattern="uv pip install 'foo>=2'"),
        Input(command="pip download requests"): Allow(),
        Input(command="python -m pip install requests"): Allow(),
        Input(command="pip install foo | tee install.log"): Allow(),
    },
)
