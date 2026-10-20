from __future__ import annotations

from captain_hook import (
    Allow,
    FilePath,
    Input,
    LambdaCondition,
    Tool,
    Warn,
    nudge,
)
from captain_hook.util.builds import LOAD_PER_CORE, HostLoad

SATURATED = {LOAD_PER_CORE: "0"}
HEALTHY = {LOAD_PER_CORE: "1000000"}
HOST_SATURATED = LambdaCondition(lambda _: HostLoad.sample().saturated)

nudge(
    "Verify test edits with the narrowest run, not the whole suite. Run `go test -run TestName ./path/to/pkg`.",
    only_if=[Tool("Edit|Write"), FilePath("*_test.go")],
    skip_if=[HOST_SATURATED],
    tests={
        Input(file="internal/cli/root_test.go", content="package cli", env=HEALTHY): Warn(),
        Input(file="internal/cli/root_test.go", content="package cli", env=SATURATED): Allow(),
        Input(file="internal/cli/root.go", content="package cli"): Allow(),
    },
)
