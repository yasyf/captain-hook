from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from cc_transcript.ids import SessionId

from captain_hook.app import _state, on
from captain_hook.cli import dispatch_event
from captain_hook.events import BaseHookEvent
from captain_hook.session import ensure_session
from captain_hook.snapshots.client import (
    CURRENT_CLIENT,
    HOOK_TAIL_BYTES,
    EvidenceIncomplete,
    SnapshotClient,
    foreground_seconds,
)
from captain_hook.testing.helpers import fixture_line
from captain_hook.testing.snapshots import FixtureOwner
from captain_hook.types import Event, HookResult, LambdaCondition, RanCommand
from captain_hook.util import reqenv
from tests.helpers import raw_assistant, raw_text, raw_text_block, raw_tool_result, raw_tool_use

TURNS = 120
PAYLOAD = 4096
SESSION = "budget-session"
DEADLINE_SECONDS = 30.0
TOOL_SECONDS = foreground_seconds(Event.PreToolUse.name)
WARM_ROUND_TRIPS = {False: 7, True: 8}

GUARDS: dict[str, Callable[[BaseHookEvent, str], bool]] = {
    "user_text": lambda evt, marker: marker in evt.ctx.t.user_text,
    "assistant_text": lambda evt, marker: marker in evt.ctx.t.assistant_text(n=10_000),
    "has_read": lambda evt, marker: evt.ctx.t.has_read("mod0.py", subagents=False),
    "has_command": lambda evt, marker: evt.ctx.t.has_command("git", "push", subagents=False),
    "gate_prompt_block": lambda evt, marker: f"result-{marker}" in evt.ctx.transcript_block(tool_results=True),
    "latest_prompt": lambda evt, marker: marker in evt.ctx.t.prompts("last", 1)[0],
    "failures": lambda evt, marker: evt.ctx.t.count_failures() > 0,
    "deep_tool": lambda evt, marker: (
        evt.ctx.t.has_tool("WebFetch", subagents=True) and not evt.ctx.t.has_tool("WebFetch", subagents=False)
    ),
}

PHASES: dict[str, Callable[[Transcript], None]] = {
    "cold": lambda transcript: None,
    "warm": lambda transcript: None,
    "append": lambda transcript: transcript.append("bravo"),
    "rewrite": lambda transcript: transcript.write("charlie", TURNS // 2),
}


@dataclass
class Transcript:
    path: Path
    sidechain: bool
    marker: str = "alpha"
    lines: list[dict[str, Any]] = field(default_factory=list)

    def turns(self, marker: str, count: int) -> list[dict[str, Any]]:
        return [
            line | {"isSidechain": self.sidechain}
            for turn in range(count)
            for line in (
                raw_text("user", f"prompt {turn} {marker}"),
                raw_assistant(
                    raw_text_block(f"reply {turn} {marker}"),
                    raw_tool_use("Read", {"file_path": f"src/mod{turn}.py"}, f"{marker}-read-{turn}"),
                ),
                raw_tool_result(f"{marker}-read-{turn}", f"result-{marker} " + "x" * PAYLOAD),
                raw_assistant(
                    raw_tool_use("Bash", {"command": "git push origin HEAD"}, f"{marker}-bash-{turn}"),
                    raw_tool_use("Grep", {"pattern": marker}, f"{marker}-grep-{turn}"),
                ),
                raw_tool_result(f"{marker}-bash-{turn}", "pushed"),
                raw_tool_result(f"{marker}-grep-{turn}", "no matches", is_error=True),
            )
        ]

    @property
    def child(self) -> Path:
        return self.path.with_suffix("") / "subagents" / "agent-child.jsonl"

    def write(self, marker: str, count: int = TURNS) -> None:
        self.marker = marker
        self.lines = self.turns(marker, count)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.render(0, self.lines))
        self.child.parent.mkdir(parents=True, exist_ok=True)
        self.child.write_text(
            self.render(
                0,
                [
                    raw_text("user", f"fetch {marker}") | {"isSidechain": True},
                    raw_assistant(raw_tool_use("WebFetch", {"url": "https://example.com"}, f"{marker}-fetch"))
                    | {"isSidechain": True},
                    raw_tool_result(f"{marker}-fetch", "fetched") | {"isSidechain": True},
                ],
            )
        )

    def append(self, marker: str, count: int = 2) -> None:
        self.marker = marker
        appended = self.turns(marker, count)
        with self.path.open("a") as handle:
            handle.write(self.render(len(self.lines), appended))
        self.lines += appended

    @staticmethod
    def render(start: int, lines: list[dict[str, Any]]) -> str:
        return "".join(json.dumps(fixture_line(start + index, line)) + "\n" for index, line in enumerate(lines))


@pytest.fixture
def owner() -> Iterator[FixtureOwner]:
    fixture = FixtureOwner()
    try:
        yield fixture
    finally:
        fixture.close()


@pytest.fixture(params=["configured", "lane"])
def transcript(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Transcript:
    if request.param == "configured":
        monkeypatch.setattr(_state, "classifier", lambda event: event.text.startswith("prompt "))
        return Transcript(tmp_path / "session.jsonl", sidechain=False)
    return Transcript(tmp_path / "session" / "subagents" / "agent-lane.jsonl", sidechain=True)


def register(event: Event, transcript: Transcript, name: str, transcript_events: int | None = None) -> list[str]:
    denied: list[str] = []
    evidence = GUARDS[name]

    def guard(evt: BaseHookEvent) -> HookResult | None:
        if not evidence(evt, transcript.marker):
            return None
        denied.append(name)
        return evt.block(f"{name} guard")

    guard.__name__ = name
    on(event, transcript_events=transcript_events)(guard)
    return denied


class HeldExchange:
    def __init__(self, owner: FixtureOwner, operation: str | None) -> None:
        self.owner = owner
        self.operation = operation
        self.operations: list[str] = []
        self.deadline_unix_ms: int | None = None

    def __call__(self, wrapper: dict[str, object]) -> dict[str, Any]:
        request = wrapper["request"]
        assert isinstance(request, dict)
        self.operations.append(request["operation"])
        if "deadline_unix_ms" in request:
            self.deadline_unix_ms = request["deadline_unix_ms"]
        if request["operation"] == self.operation:
            assert self.deadline_unix_ms is not None
            while time.time() * 1000 <= self.deadline_unix_ms + 1:
                time.sleep(0.001)
        return self.owner.exchange(wrapper)


def dispatch(
    owner: FixtureOwner,
    root: Path,
    event: Event,
    transcript: Transcript,
    exchange: Callable[[dict[str, object]], dict[str, Any]] | None = None,
    seconds: float = DEADLINE_SECONDS,
) -> tuple[object, list[str]]:
    client = SnapshotClient(exchange or owner.exchange, foreground_seconds=seconds)
    payload = {"session_id": SESSION, "transcript_path": str(transcript.path), "cwd": str(root)} | (
        {"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}} if event is Event.PreToolUse else {}
    )
    request = reqenv.RequestOverrides(env={}, cwd=str(root), client_ppid=1, session_id=SESSION)
    token = CURRENT_CLIENT.set(client)
    try:
        with reqenv.use_request(request):
            envelope, _ = dispatch_event(root, event, payload, session_dir=ensure_session(SessionId(SESSION)))
    finally:
        CURRENT_CLIENT.reset(token)
        client.close()
    return envelope, request.evidence_gaps


@pytest.mark.parametrize("guard", list(GUARDS))
@pytest.mark.parametrize("event", [Event.PreToolUse, Event.Stop], ids=["tool", "turn"])
@pytest.mark.parametrize("phase", list(PHASES))
def test_a_transcript_guard_blocks_within_the_foreground_deadline(
    tmp_path: Path, owner: FixtureOwner, transcript: Transcript, event: Event, phase: str, guard: str
) -> None:
    transcript.write("alpha")
    denied = register(event, transcript, guard)
    if phase != "cold":
        dispatch(owner, tmp_path, event, transcript)
        denied.clear()
    PHASES[phase](transcript)

    envelope, gaps = dispatch(owner, tmp_path, event, transcript)

    assert gaps == []
    assert denied == [guard]
    assert envelope is not None


def test_a_foreground_dispatch_never_renews_a_lease_the_owner_cannot_extend(
    tmp_path: Path, owner: FixtureOwner, transcript: Transcript
) -> None:
    transcript.write("alpha")
    denied = register(Event.PreToolUse, transcript, "has_command")
    dispatch(owner, tmp_path, Event.PreToolUse, transcript)
    denied.clear()
    exchange = HeldExchange(owner, None)

    _, gaps = dispatch(owner, tmp_path, Event.PreToolUse, transcript, exchange, TOOL_SECONDS)

    assert gaps == []
    assert denied == ["has_command"]
    assert "renew" not in exchange.operations
    assert len(exchange.operations) == WARM_ROUND_TRIPS[transcript.sidechain]


def test_a_retain_answered_after_the_foreground_deadline_is_a_deadline_gap(
    tmp_path: Path, owner: FixtureOwner, transcript: Transcript
) -> None:
    transcript.write("alpha")
    denied = register(Event.PreToolUse, transcript, "has_command")
    exchange = HeldExchange(owner, "retain")

    envelope, gaps = dispatch(owner, tmp_path, Event.PreToolUse, transcript, exchange, TOOL_SECONDS)

    assert gaps == ["has_command: deadline: lease expired at the foreground transcript deadline"]
    assert denied == []
    assert envelope is None


def test_a_late_lease_skips_only_the_hook_that_read_it(
    tmp_path: Path, owner: FixtureOwner, transcript: Transcript
) -> None:
    transcript.write("alpha")
    denied = register(Event.PreToolUse, transcript, "has_command")

    @on(Event.PreToolUse)
    def policy(evt: BaseHookEvent) -> HookResult:
        denied.append("policy")
        return evt.block("policy guard")

    exchange = HeldExchange(owner, "retain")

    envelope, gaps = dispatch(owner, tmp_path, Event.PreToolUse, transcript, exchange, TOOL_SECONDS)

    assert gaps == ["has_command: deadline: lease expired at the foreground transcript deadline"]
    assert denied == ["policy"]
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_async_hooks_read_the_transcript_after_the_foreground_deadline(
    tmp_path: Path, owner: FixtureOwner, transcript: Transcript
) -> None:
    transcript.write("alpha")
    denied = register(Event.PostToolUse, transcript, "has_command")
    read: list[bool] = []

    @on(Event.PostToolUse, async_=True)
    def late(evt: BaseHookEvent) -> None:
        read.append(evt.ctx.t.has_command("git", "push", subagents=False))

    foreground = SnapshotClient(owner.exchange, foreground_seconds=TOOL_SECONDS)
    background = SnapshotClient(owner.exchange)
    payload = {
        "session_id": SESSION,
        "transcript_path": str(transcript.path),
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin HEAD"},
    }
    request = reqenv.RequestOverrides(env={}, cwd=str(tmp_path), client_ppid=1, session_id=SESSION)
    token = CURRENT_CLIENT.set(foreground)
    try:
        with reqenv.use_request(request):
            _, after = dispatch_event(
                tmp_path, Event.PostToolUse, payload, session_dir=ensure_session(SessionId(SESSION))
            )
            assert foreground.foreground_deadline_unix_ms is not None
            while time.time() * 1000 <= foreground.foreground_deadline_unix_ms + 1:
                time.sleep(0.01)
            CURRENT_CLIENT.set(background)
            after()
    finally:
        CURRENT_CLIENT.reset(token)
        foreground.close()
        background.close()

    assert denied == ["has_command"]
    assert read == [True]
    assert request.evidence_gaps == []


def test_an_async_hook_runs_beside_a_sibling_whose_evidence_is_stale(tmp_path: Path) -> None:
    ran: list[str] = []

    @on(Event.PostToolUse, async_=True)
    def stale(evt: BaseHookEvent) -> None:
        raise EvidenceIncomplete("stale_handle", "lease does not belong to this claimant or generation")

    @on(Event.PostToolUse, async_=True, only_if=[LambdaCondition(lambda evt: stale_condition())])
    def stale_gate(evt: BaseHookEvent) -> None:
        ran.append("stale_gate")

    @on(Event.PostToolUse, async_=True)
    def answers(evt: BaseHookEvent) -> None:
        ran.append("answers")

    request = reqenv.RequestOverrides(env={}, cwd=str(tmp_path), client_ppid=1, session_id=SESSION)
    with reqenv.use_request(request):
        _, after = dispatch_event(
            tmp_path, Event.PostToolUse, {"session_id": SESSION, "tool_name": "AskUserQuestion"}, session_dir=None
        )
        after()

    assert ran == ["answers"]
    assert sorted(request.evidence_gaps) == [
        "stale: stale_handle: lease does not belong to this claimant or generation",
        "stale_gate: stale_handle: condition lease expired",
    ]


def stale_condition() -> bool:
    raise EvidenceIncomplete("stale_handle", "condition lease expired")


class Operations:
    def __init__(self, owner: FixtureOwner) -> None:
        self.owner = owner
        self.seen: list[str] = []

    def __call__(self, wrapper: dict[str, object]) -> dict[str, Any]:
        request = wrapper["request"]
        assert isinstance(request, dict)
        self.seen.append(request["operation"])
        return self.owner.exchange(wrapper)


@pytest.mark.parametrize("guard", ["has_command", "user_text"])
def test_a_declared_window_reads_only_the_transcript_tail(
    tmp_path: Path, owner: FixtureOwner, transcript: Transcript, guard: str
) -> None:
    transcript.write("alpha", TURNS * 4)
    transcript.append("bravo")
    denied = register(Event.PreToolUse, transcript, guard, transcript_events=12)
    operations = Operations(owner)

    envelope, gaps = dispatch(owner, tmp_path, Event.PreToolUse, transcript, operations, TOOL_SECONDS)

    assert gaps == []
    assert denied == [guard]
    assert envelope is not None
    assert operations.seen == ["tail"]


def test_a_declared_window_answers_while_a_full_load_runs_out_the_deadline(
    tmp_path: Path, owner: FixtureOwner, transcript: Transcript
) -> None:
    transcript.write("alpha")
    fired: list[str] = []

    @on(Event.PreToolUse, skip_if=[RanCommand("never", "ran")])
    def history(evt: BaseHookEvent) -> None:
        fired.append("history")

    @on(Event.PreToolUse, skip_if=[RanCommand("ccx", "vcs", "status")], transcript_events=12)
    def recent(evt: BaseHookEvent) -> HookResult:
        fired.append("recent")
        return evt.block("recent guard")

    exchange = HeldExchange(owner, "acquire")

    envelope, gaps = dispatch(owner, tmp_path, Event.PreToolUse, transcript, exchange, TOOL_SECONDS)

    assert fired == ["recent"]
    assert [gap.partition(":")[0] for gap in gaps] == ["history"]
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


class Requests:
    def __init__(self, owner: FixtureOwner) -> None:
        self.owner = owner
        self.seen: list[dict[str, Any]] = []

    def __call__(self, wrapper: dict[str, object]) -> dict[str, Any]:
        request = wrapper["request"]
        assert isinstance(request, dict)
        self.seen.append(request)
        return self.owner.exchange(wrapper)


def test_an_undeclared_hook_acquires_only_the_transcript_tail(
    tmp_path: Path, owner: FixtureOwner, transcript: Transcript
) -> None:
    transcript.write("alpha", TURNS * 12)
    transcript.append("bravo")
    assert transcript.path.stat().st_size > HOOK_TAIL_BYTES
    denied = register(Event.Stop, transcript, "user_text")
    requests = Requests(owner)

    envelope, gaps = dispatch(owner, tmp_path, Event.Stop, transcript, requests)

    acquires = [request for request in requests.seen if request["operation"] == "acquire"]
    assert gaps == []
    assert denied == ["user_text"]
    assert envelope is not None
    assert acquires
    assert {request["tail_bytes"] for request in acquires} == {HOOK_TAIL_BYTES}
