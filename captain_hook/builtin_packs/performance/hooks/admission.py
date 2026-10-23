from __future__ import annotations

from typing import TYPE_CHECKING

from captain_hook import Allow, Event, HookResult, Input, Tool, Warn, on
from captain_hook.util.builds import LOAD_PER_CORE, HostLoad, is_local_build

if TYPE_CHECKING:
    from captain_hook import PreToolUseEvent

SATURATED = {LOAD_PER_CORE: "0"}
HEALTHY = {LOAD_PER_CORE: "1000000"}


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash")],
    max_fires=1,
    tests={
        Input(command="go test ./...", env=SATURATED): Warn(pattern="CI builds and tests"),
        Input(command="go test ./...", env=HEALTHY): Allow(),
        Input(command="env GOFLAGS=-p=1 timeout 600 /opt/homebrew/bin/go vet ./...", env=SATURATED): Warn(),
        Input(command="cargo +stable check --tests", env=SATURATED): Warn(),
        Input(command="bash -lc 'cargo build --release'", env=SATURATED): Warn(),
        Input(command="ssh forge true && go build ./...", env=SATURATED): Warn(),
        Input(command="ssh forge go build ./...", env=SATURATED): Allow(),
        Input(command="ssh forge 'cd repo && cargo test'", env=SATURATED): Allow(),
        Input(command="remote-builder exec --lane go -- go test ./...", env=SATURATED): Allow(),
        Input(command="go version && cargo metadata --format-version 1", env=SATURATED): Allow(),
        Input(command="git commit -m 'go build fix'", env=SATURATED): Allow(),
    },
)
def admit_local_build(evt: PreToolUseEvent) -> HookResult | None:
    if not any(is_local_build(call) for call in evt.cmd.calls()):
        return None
    if not HostLoad.sample().saturated:
        return None
    return evt.warn("CI builds and tests; do not run local builds or tests.")
