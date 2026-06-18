from __future__ import annotations

import re

from captain_hook import Allow, Block, Event, Input, Tool, block_command, nudge
from captain_hook.events import PostToolUseFailureEvent

block_command(
    r"^gofumpt\b",
    reason="Do not run gofumpt manually — formatting is applied by the commit hook (golangci-lint fmt)",
    hint="See AGENTS.md § Mechanical Linting. The prek hook formats on commit; `task fmt` runs it deliberately.",
    tests={
        Input(command="gofumpt -w ."): Block(),
        Input(command="go build ./..."): Allow(),
    },
)

block_command(
    r"^golangci-lint\b",
    reason="Do not run golangci-lint manually — CI and the commit hook own linting",
    hint="See AGENTS.md § Mechanical Linting. Only fix issues requiring human judgment.",
    tests={
        Input(command="golangci-lint run"): Block(),
        Input(command="golangci-lint fmt"): Block(),
        Input(command="go vet ./..."): Allow(),
        Input(command="uvx prek run --all-files"): Allow(),
    },
)

nudge(
    "MISSING DEPENDENCY: run `go mod tidy` (or `go get <module>`) to resolve it. "
    "Do NOT make the import lazy, delete the importing code, or vendor by hand.",
    events=Event.PostToolUseFailure,
    only_if=[Tool("Bash")],
    when=lambda evt: (
        isinstance(evt, PostToolUseFailureEvent)
        and bool(
            re.search(
                r"no required module provides package|missing go\.sum entry|"
                r"cannot find module|updates to go\.mod needed",
                evt.error,
            )
        )
    ),
    max_fires=2,
)
