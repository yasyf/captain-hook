from __future__ import annotations

from typing import TYPE_CHECKING

from captain_hook import Allow, Block, Event, HookResult, Input, Tool, on
from captain_hook.util.builds import LOAD_PER_CORE, HostLoad, is_local_build

if TYPE_CHECKING:
    from captain_hook import PreToolUseEvent

SATURATED = {LOAD_PER_CORE: "0"}
HEALTHY = {LOAD_PER_CORE: "1000000"}


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash")],
    tests={
        Input(command="go test ./...", env=SATURATED): Block(pattern="host is saturated"),
        Input(command="go test ./...", env=HEALTHY): Allow(),
        Input(command="env GOFLAGS=-p=1 timeout 600 /opt/homebrew/bin/go vet ./...", env=SATURATED): Block(),
        Input(command="cargo +stable check --tests", env=SATURATED): Block(pattern="remote-builder exec"),
        Input(command="bash -lc 'cargo build --release'", env=SATURATED): Block(),
        Input(command="ssh forge true && go build ./...", env=SATURATED): Block(),
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
    if not (host := HostLoad.sample()).saturated:
        return None
    return evt.block(
        f"The host is saturated at load {host.load:.0f} on {host.cores} cores, so local Go and Cargo builds and tests "
        "wait for CI. Run already-approved remote work explicitly with `ssh <host> '<command>'` or "
        "`remote-builder exec --lane go|cargo -- <command>`, and never retry on a timer."
    )
