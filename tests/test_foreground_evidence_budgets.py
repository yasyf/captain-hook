from __future__ import annotations

import json
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
from captain_hook.snapshots.client import CURRENT_CLIENT, SnapshotClient, foreground_allowance
from captain_hook.testing.helpers import fixture_line
from captain_hook.testing.snapshots import FixtureOwner
from captain_hook.types import Event, HookResult
from captain_hook.util import reqenv
from tests.helpers import raw_assistant, raw_text, raw_text_block, raw_tool_result, raw_tool_use

TURNS = 120
PAYLOAD = 4096
SESSION = "budget-session"
DEADLINE_SECONDS = 30.0

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


def register(event: Event, transcript: Transcript, name: str) -> list[str]:
    denied: list[str] = []
    evidence = GUARDS[name]

    def guard(evt: BaseHookEvent) -> HookResult | None:
        if not evidence(evt, transcript.marker):
            return None
        denied.append(name)
        return evt.block(f"{name} guard")

    guard.__name__ = name
    on(event)(guard)
    return denied


def dispatch(owner: FixtureOwner, root: Path, event: Event, transcript: Transcript) -> tuple[object, list[str]]:
    _, source_read_bytes = foreground_allowance(event.name)
    client = SnapshotClient(
        owner.exchange, foreground_seconds=DEADLINE_SECONDS, foreground_source_read_bytes=source_read_bytes
    )
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
def test_a_transcript_guard_blocks_within_the_foreground_read_allowance(
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
