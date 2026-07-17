"""The acceptance gate: every event served warm through ``hook`` must be
byte-identical to the cold ``python -m captain_hook`` CLI — same stdout, same stderr, same
exit code. The client runs under ``CAPT_HOOK_DAEMON_FALLBACK=closed`` so a daemon bug surfaces
as a divergent (or exit-1) response, never silently masked as a cold fallback. ``run_cold`` and
``run_client`` are handed the identical environment (worker key, state, cache, decisions).

The socket-level suite (``test_daemon_server``) proves the server's response bytes; this suite
proves the whole client→socket→cold-parity contract end to end across the event surface.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.daemon_helpers import (
    cleanup_dirs,
    daemon_dirs,
    daemon_env,
    make_project,
    run_client,
    run_cold,
    running_daemon,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

STOP_TRANSCRIPT = str(Path(__file__).resolve().parent / "fixtures" / "hook_fires" / "fire-stop.jsonl")

# One hooks module covering every parity case. No capt-hook.toml and an isolated (plugin-free) config,
# so discovery loads only these hooks — deterministic output on both the warm and cold paths.
PARITY_HOOK_SRC = """
from __future__ import annotations

from captain_hook import Event, Tool, approve, deny, hook, on

hook(Event.PreToolUse, only_if=[Tool("Edit")], message="pre-tool warning")

approve("read-allow", only_if=[Tool("Read")])
approve("write-allow", only_if=[Tool("Write")])
approve("grep-allow", only_if=[Tool("Grep")])
deny("no bash here", only_if=[Tool("Bash")])

hook(Event.PreToolUse, only_if=[Tool("Grep")], message="grep advisory")


@on(Event.PreToolUse, only_if=[Tool("Write")])
def sanitize_write(evt):
    return evt.rewrite({"file_path": "/tmp/sanitized.py", "content": "x"})


@on(Event.PostToolUse)
def say(evt):
    ti = evt._raw.get("tool_input", {})
    if ti.get("say"):
        print("HELLO_FROM_HOOK")
    if ti.get("big"):
        print("\\u2603" * 12000)
    return None


@on(Event.PostToolUse, async_=True)
def async_probe(evt):
    if evt._raw.get("tool_input", {}).get("asay"):
        print("ASYNC_FIRED")
    return None


@on(Event.Stop)
def stop_probe(evt):
    if evt.ctx.transcript.has_command("date"):
        print("STOP_SAW_DATE")
    return None


@on(Event.UserPromptSubmit)
def ups(evt):
    print("UPS_FIRED")
    return None
"""


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    event: str
    payload: dict[str, Any] | str
    async_: bool = False
    nonempty: bool = False
    empty: bool = False
    stderr_prefix: str | None = None
    exit_code: int | None = None

    def stdin(self) -> bytes:
        return (self.payload if isinstance(self.payload, str) else json.dumps(self.payload)).encode()

    def args(self, root: Path) -> tuple[str, ...]:
        return ("--root", str(root), "run", self.event, *(("--async",) if self.async_ else ()))

    def check(self, result: subprocess.CompletedProcess[bytes]) -> None:
        if self.nonempty:
            assert result.stdout != b"", "expected the hook to produce output"
        if self.empty:
            assert result.stdout == b"" and result.stderr == b"", "expected a silent no-op"
        if self.stderr_prefix is not None:
            assert result.stderr.startswith(self.stderr_prefix.encode()), result.stderr
        if self.exit_code is not None:
            assert result.returncode == self.exit_code


CASES = [
    Case(
        "pretooluse_match",
        "PreToolUse",
        {
            "session_id": "p1",
            "tool_name": "Edit",
            "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"},
        },
        nonempty=True,
    ),
    Case(
        "pretooluse_no_match",
        "PreToolUse",
        {"session_id": "p2", "tool_name": "Glob", "tool_input": {"pattern": "*.py"}},
        empty=True,
        exit_code=0,
    ),
    Case(
        "pretooluse_deny",
        "PreToolUse",
        {"session_id": "p2d", "tool_name": "Bash", "tool_input": {"command": "ls"}},
        nonempty=True,
    ),
    Case(
        "pretooluse_rewrite_over_allow",
        "PreToolUse",
        {"session_id": "p2r", "tool_name": "Write", "tool_input": {"file_path": "/etc/x.py", "content": "y"}},
        nonempty=True,
    ),
    Case(
        "pretooluse_allow_with_warning",
        "PreToolUse",
        {"session_id": "p2w", "tool_name": "Grep", "tool_input": {"pattern": "x"}},
        nonempty=True,
    ),
    Case(
        "posttooluse",
        "PostToolUse",
        {"session_id": "p3", "tool_name": "Bash", "tool_input": {"say": True}},
        nonempty=True,
    ),
    Case(
        "stop_with_transcript_condition",
        "Stop",
        {"session_id": "p4", "stop_hook_active": False, "transcript_path": STOP_TRANSCRIPT},
        nonempty=True,
    ),
    Case(
        "permission_allow",
        "PermissionRequest",
        {"session_id": "p5", "tool_name": "Read", "tool_input": {"file_path": "x.py"}},
        nonempty=True,
    ),
    Case(
        "permission_deny",
        "PermissionRequest",
        {"session_id": "p6", "tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
        nonempty=True,
    ),
    Case("user_prompt_submit", "UserPromptSubmit", {"session_id": "p7", "prompt": "hello"}, nonempty=True),
    Case(
        "posttooluse_async",
        "PostToolUse",
        {"session_id": "p8", "tool_name": "Bash", "tool_input": {"asay": True}},
        async_=True,
        nonempty=True,
    ),
    Case(
        "unicode_over_10k",
        "PostToolUse",
        {"session_id": "p9", "tool_name": "Bash", "tool_input": {"big": True}},
        nonempty=True,
    ),
    Case("malformed_stdin", "PostToolUse", "{not valid json", stderr_prefix="Malformed stdin:", exit_code=0),
    Case("empty_stdin", "PostToolUse", "", empty=True, exit_code=0),
    Case("unknown_event", "Nonsense", {"session_id": "p12"}, stderr_prefix="Invalid event type:", exit_code=1),
]


@pytest.fixture(scope="module")
def parity() -> Iterator[tuple[Path, dict[str, str]]]:
    dirs = daemon_dirs()
    root = make_project(Path(tempfile.mkdtemp(prefix="chp-proj-")), PARITY_HOOK_SRC)
    env = daemon_env(root, dirs, CAPT_HOOK_DAEMON_FALLBACK="closed")
    try:
        with running_daemon(root, env):
            yield root, env
    finally:
        shutil.rmtree(root, ignore_errors=True)
        cleanup_dirs(dirs)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_warm_dispatch_is_byte_identical_to_cold(parity: tuple[Path, dict[str, str]], case: Case) -> None:
    root, env = parity
    stdin, args = case.stdin(), case.args(root)
    cold = run_cold(*args, env=env, stdin=stdin, cwd=str(root))
    warm = run_client(*args, env=env, stdin=stdin, cwd=str(root))
    assert (warm.returncode, warm.stdout, warm.stderr) == (cold.returncode, cold.stdout, cold.stderr), (
        f"warm/cold divergence for {case.id}:\n"
        f"  cold: rc={cold.returncode} out={cold.stdout!r} err={cold.stderr!r}\n"
        f"  warm: rc={warm.returncode} out={warm.stdout!r} err={warm.stderr!r}"
    )
    case.check(cold)


# A hook module that prints to stdout at import time, exercising the discovery-stdout replay path.
IMPORT_STDOUT_HOOK_SRC = """
from __future__ import annotations

from captain_hook import Event, on

print("DISCOVERY_STDOUT_LINE")


@on(Event.PostToolUse)
def noop(evt):
    return None
"""


@pytest.fixture(scope="module")
def parity_import_stdout() -> Iterator[tuple[Path, dict[str, str]]]:
    dirs = daemon_dirs()
    root = make_project(Path(tempfile.mkdtemp(prefix="chp-imp-")), IMPORT_STDOUT_HOOK_SRC)
    env = daemon_env(root, dirs, CAPT_HOOK_DAEMON_FALLBACK="closed")
    try:
        with running_daemon(root, env):
            yield root, env
    finally:
        shutil.rmtree(root, ignore_errors=True)
        cleanup_dirs(dirs)


def test_warm_replays_import_time_stdout(parity_import_stdout: tuple[Path, dict[str, str]]) -> None:
    # R3: an import-time print lands on discovery stdout; warm must replay it byte-identical to cold on
    # both streams (without the fix the daemon captured but never replayed it, so warm.stdout was empty).
    root, env = parity_import_stdout
    case = Case("import_stdout", "PostToolUse", {"session_id": "imp1", "tool_name": "Bash", "tool_input": {}})
    stdin, args = case.stdin(), case.args(root)
    cold = run_cold(*args, env=env, stdin=stdin, cwd=str(root))
    warm = run_client(*args, env=env, stdin=stdin, cwd=str(root))
    assert cold.stdout == b"DISCOVERY_STDOUT_LINE\n"  # cold prints at import, before the (empty) decision
    assert (warm.returncode, warm.stdout, warm.stderr) == (cold.returncode, cold.stdout, cold.stderr), (
        f"warm/cold divergence:\n  cold: out={cold.stdout!r} err={cold.stderr!r}\n"
        f"  warm: out={warm.stdout!r} err={warm.stderr!r}"
    )
