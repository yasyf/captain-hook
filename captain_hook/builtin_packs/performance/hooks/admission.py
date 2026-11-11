from __future__ import annotations

from typing import TYPE_CHECKING

from captain_hook import Allow, Block, Event, HookResult, Input, Tool, on
from captain_hook.util.builds import LOAD_PER_CORE, ROUTE_PROBE, HostLoad, is_local_build, probe_route

if TYPE_CHECKING:
    from captain_hook import PreToolUseEvent

SATURATED = {LOAD_PER_CORE: "0"}
HEALTHY = {LOAD_PER_CORE: "1000000"}
ROUTED = {**SATURATED, ROUTE_PROBE: "true"}
ONLY_CARGO_ROUTES = {**SATURATED, ROUTE_PROBE: """sh -c '[ "$0" = cargo ]'"""}
HATCH_KEEPS_IT_HERE = {**SATURATED, ROUTE_PROBE: """sh -c '[ -z "${CARGO_LOCAL:-}" ]'"""}
NAMES_A_BUILDER = {
    **SATURATED,
    ROUTE_PROBE: """sh -c 'echo "remote-builder exec --lane $0 --machine mac -- $0 $*"; exit 1'""",
}
ROUTES_AND_NAMES_IT = {
    **SATURATED,
    ROUTE_PROBE: """sh -c 'echo "remote-builder exec --lane $0 --machine linux -- $0 $*"'""",
}


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
        Input(command="cargo check", env={**SATURATED, ROUTE_PROBE: "false"}): Block(pattern="Rerun the same command"),
        Input(command="cargo check", env={**SATURATED, ROUTE_PROBE: "no-such-route-probe"}): Block(),
        Input(command="/opt/homebrew/bin/cargo check", env=ROUTED): Block(),
        Input(command="timeout 600 cargo check", env=ROUTED): Block(),
        Input(command="cargo check && go test ./...", env=ONLY_CARGO_ROUTES): Block(),
        Input(command="cargo check", env=ONLY_CARGO_ROUTES): Allow(),
        Input(command="CARGO_LOCAL=1 cargo check", env=HATCH_KEEPS_IT_HERE): Block(),
        Input(command="cargo check", env=HATCH_KEEPS_IT_HERE): Allow(),
        Input(command="go vet ./...", env=NAMES_A_BUILDER): Block(
            pattern=r"now as below.*\n+```\nremote-builder exec --lane go --machine mac -- go vet \./\.\.\.\n```"
        ),
        Input(command="cd /tmp && go vet ./...", env=NAMES_A_BUILDER): Block(
            pattern=r"```\ncd \S*tmp && remote-builder exec --lane go --machine mac -- go vet"
        ),
        Input(command="timeout 900 cargo check -p core", env=ROUTES_AND_NAMES_IT): Block(
            pattern=r"```\nremote-builder exec --lane cargo --machine linux -- cargo check -p core\n```"
        ),
        Input(command="cargo check -p core", env=ROUTES_AND_NAMES_IT): Allow(),
    },
)
def admit_local_build(evt: PreToolUseEvent) -> HookResult | None:
    builds = [call for call in evt.cmd.calls() if is_local_build(call)]
    if not builds:
        return None
    host = HostLoad.sample()
    if not host.saturated:
        return None
    routes = [probe_route(call) for call in builds]
    if all(route.leaves for route in routes):
        return None
    gate = (
        f"The host's CPU is {host.load / host.cores:.0%} busy, at or above the {host.limit:.0%} limit; "
        "this is a load gate, not a ban on local builds."
    )
    elsewhere = "\n".join(route.elsewhere for route in routes if not route.leaves and route.elsewhere)
    if not elsewhere:
        return evt.block(f"{gate} Rerun the same command once it drops; CI still grades tests.")
    return evt.block(
        f"{gate} Run it on another host now as below, or rerun it once the load drops.\n\n```\n{elsewhere}\n```"
    )
