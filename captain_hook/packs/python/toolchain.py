from __future__ import annotations

import re

from captain_hook import Allow, Block, Event, Input, Tool, block_command, nudge
from captain_hook.events import PostToolUseFailureEvent

block_command(
    r"^ruff\b",
    reason="Do not run ruff manually — mechanical linting is auto-fixed by tooling",
    hint="See AGENTS.md § Mechanical Linting. Only fix issues requiring human judgment.",
    tests={
        Input(command="ruff check ."): Block(),
        Input(command="ruff format ."): Block(),
        Input(command="prek run --all-files"): Allow(),
        Input(command="uvx prek run --all-files"): Allow(),
        Input(command="uvx prek run ty --all-files"): Allow(),
    },
)

nudge(
    "MISSING DEPENDENCY: Run `uv sync --extra dev` (or `uv pip install <package>`) to fix this. "
    "Do NOT make imports lazy, remove the importing code, or restructure "
    "code to avoid the import.",
    events=Event.PostToolUseFailure,
    only_if=[Tool("Bash")],
    when=lambda evt: (
        isinstance(evt, PostToolUseFailureEvent)
        and bool(re.search(r"ModuleNotFoundError|ImportError: (?:cannot import|No module named)", evt.error))
    ),
    max_fires=2,
)
