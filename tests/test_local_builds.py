from __future__ import annotations

import pytest
from cc_transcript.command import parse_command_line

from captain_hook.cmd import Cmd
from captain_hook.util.builds import is_local_build


@pytest.mark.parametrize(
    "command",
    [
        "go build ./...",
        "go test -run TestOne ./internal/runtime",
        "go vet ./internal/runtime-api/...",
        "go -C /work/runtime build ./...",
        "go -C test vet ./...",
        "/Users/dev/.local/share/mise/installs/go/1.26.9/bin/go build -o /dev/null .",
        "env GOMAXPROCS=2 timeout 30 go test ./internal/runtime",
        "cargo build --release",
        "cargo check -p sandsql-core --tests",
        "cargo test -p sandsql-core",
        "cargo +stable check -p sandsql-core",
        "cargo +1.97.1 test -p sandsql-core",
        "/Users/dev/.rustup/toolchains/stable-aarch64-apple-darwin/bin/cargo check --tests",
        "env CARGO_BUILD_JOBS=2 timeout 30 cargo check --tests",
        "bash -lc 'go build ./... && cargo check --tests'",
        "eval 'go vet ./...'",
        "echo $(go build ./...)",
        "ssh forge true && go build ./...",
        "ssh forge 'cargo check' && cargo test",
        "go run golang.org/x/tools/cmd/deadcode@latest ./...",
        "go run golang.org/x/tools/cmd/deadcode@v0.38.0 ./...",
    ],
)
def test_recognizes_local_build_invocations(command: str) -> None:
    assert any(is_local_build(call) for call in Cmd(parse_command_line(command)).calls())


@pytest.mark.parametrize(
    "command",
    [
        "ssh forge go build ./...",
        "ssh forge 'go test ./... && cargo check --tests'",
        "ssh forge 'bash -lc \"go vet ./...\"'",
        "env SSH_AUTH_SOCK=/tmp/socket timeout 30 ssh forge cargo check --tests",
        "remote-builder exec --lane go -- go test ./...",
        "/Users/dev/.local/bin/remote-builder exec --lane go -- go test ./...",
        "echo go build ./...",
        "printf '%s' 'cargo check --tests'",
        "go version",
        "go list ./...",
        "go help test",
        "go -C build version",
        "cargo --color test metadata",
        "cargo metadata --format-version 1",
        "cargo fmt --check",
        "go run ./cmd/server",
        "go run example.com/cmd/deadcode@latest ./...",
        "go run golang.org/x/tools/cmd/deadcode-extra@latest ./...",
        "",
    ],
)
def test_preserves_remote_invocations_and_other_commands(command: str) -> None:
    assert not any(is_local_build(call) for call in Cmd(parse_command_line(command)).calls())
