from __future__ import annotations

from typing import TYPE_CHECKING

from captain_hook import Allow, Block, Event, HookResult, Input, Tool, on
from captain_hook.util.builds import LOAD_PER_CORE, ROUTE_PROBE, HostLoad, is_local_build, routes_off_host

if TYPE_CHECKING:
    from captain_hook import PreToolUseEvent

SATURATED = {LOAD_PER_CORE: "0"}
HEALTHY = {LOAD_PER_CORE: "1000000"}
ROUTED = {**SATURATED, ROUTE_PROBE: "true"}
ONLY_CARGO_ROUTES = {**SATURATED, ROUTE_PROBE: """sh -c '[ "$0" = cargo ]'"""}
HATCH_KEEPS_IT_HERE = {**SATURATED, ROUTE_PROBE: """sh -c '[ -z "${CARGO_LOCAL:-}" ]'"""}


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash")],
    tests={
        Input(command="go test ./...", env=SATURATED): Block(pattern="load gate"),
        Input(command="go test ./...", env=HEALTHY): Allow(),
        Input(command="go build ./next/...", env=SATURATED): Block(),
        Input(command="go build -o /dev/null ./next/cmd/runtime-api ./next/internal/iris", env=SATURATED): Block(),
        Input(command="go vet ./next/internal/iris/", env=SATURATED): Block(),
        Input(command="/Users/dev/.local/share/mise/installs/go/1.26.9/bin/go build ./...", env=SATURATED): Block(),
        Input(
            command="env CGO_ENABLED=1 timeout 600 /Users/dev/.local/share/mise/installs/go/1.26.9/bin/go vet ./...",
            env=SATURATED,
        ): Block(),
        Input(command="env GOFLAGS=-p=1 timeout 600 /opt/homebrew/bin/go vet ./...", env=SATURATED): Block(),
        Input(command="cargo +stable check --tests", env=SATURATED): Block(),
        Input(command="bash -lc 'cargo build --release'", env=SATURATED): Block(),
        Input(command="ssh forge true && go build ./...", env=SATURATED): Block(),
        Input(command="ssh forge go build ./...", env=SATURATED): Allow(),
        Input(command="ssh forge 'cd repo && cargo test'", env=SATURATED): Allow(),
        Input(command="remote-builder exec --lane go -- go test ./...", env=SATURATED): Allow(),
        Input(command="go version && cargo metadata --format-version 1", env=SATURATED): Allow(),
        Input(command="git commit -m 'go build fix'", env=SATURATED): Allow(),
        Input(command="cargo build -p server", env=ROUTED): Allow(),
        Input(command="cargo check", env={**SATURATED, ROUTE_PROBE: "false"}): Block(pattern="load gate"),
        Input(command="cargo check", env={**SATURATED, ROUTE_PROBE: "no-such-route-probe"}): Block(),
        Input(command="/opt/homebrew/bin/cargo check", env=ROUTED): Block(),
        Input(command="timeout 600 cargo check", env=ROUTED): Block(),
        Input(command="cargo check && go test ./...", env=ONLY_CARGO_ROUTES): Block(),
        Input(command="cargo check", env=ONLY_CARGO_ROUTES): Allow(),
        Input(command="CARGO_LOCAL=1 cargo check", env=HATCH_KEEPS_IT_HERE): Block(),
        Input(command="cargo check", env=HATCH_KEEPS_IT_HERE): Allow(),
    },
)
def admit_local_build(evt: PreToolUseEvent) -> HookResult | None:
    builds = [call for call in evt.cmd.calls() if is_local_build(call)]
    if not builds:
        return None
    host = HostLoad.sample()
    if not host.saturated or all(routes_off_host(call) for call in builds):
        return None
    return evt.block(
        f"The host's CPU is {host.load / host.cores:.0%} busy, at or above the {host.limit:.0%} limit; "
        "this is a load gate, not a ban on local builds. "
        "Rerun the same command once it drops; CI still grades tests."
    )
