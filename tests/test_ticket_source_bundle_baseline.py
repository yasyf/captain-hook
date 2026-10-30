from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

import pytest

from captain_hook import T
from captain_hook.context import HookContext
from captain_hook.events import PreToolUseEvent
from captain_hook.session import SessionStore
from captain_hook.testing.helpers import fixture_line, fixture_session
from captain_hook.tickets import Context, Proposal
from captain_hook.tickets import context as sources
from captain_hook.transcripts import ROOT_EXCERPT_BYTES


class SourceReader:
    def __init__(self, stream: BinaryIO, counts: dict[str, int]) -> None:
        self.stream = stream
        self.counts = counts

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.stream.seek(offset, whence)

    def readline(self) -> bytes:
        line = self.stream.readline()
        self.counts["bytes"] += len(line)
        self.counts["readline_calls"] += 1
        return line

    def __iter__(self) -> SourceReader:
        return self

    def __next__(self) -> bytes:
        if line := self.readline():
            return line
        raise StopIteration


def test_context_source_views_reopen_the_root_and_observe_between_view_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root.jsonl"
    old = T.user("C1 proceed with the reply", uuid="root-old")
    reply = T.assistant("I will post it", uuid="root-reply")
    messages = [
        T.assistant("x" * (ROOT_EXCERPT_BYTES + 4096)),
        T.user("gap one"),
        T.assistant("gap two"),
        T.user("gap three"),
        old,
        reply,
    ]
    with root.open("wb") as output:
        for index, message in enumerate(messages):
            output.write(json.dumps(fixture_line(index, message)).encode() + b"\n")
    reads: list[dict[str, int]] = []
    original_open = Path.open

    @contextmanager
    def counted_open(path, mode="r", *args, **kwargs):
        with original_open(path, mode, *args, **kwargs) as stream:
            if path == root and mode == "rb":
                counts = {"bytes": 0, "readline_calls": 0}
                reads.append(counts)
                yield SourceReader(stream, counts)
            else:
                yield stream

    monkeypatch.setattr(Path, "open", counted_open)
    monkeypatch.setattr(sources, "board_state", lambda tree, cwd: {})
    evt = PreToolUseEvent(
        _raw={"session_id": "lane", "tool_name": "slack_reply", "tool_use_id": "pending", "tool_input": {}},
        ctx=HookContext(
            session=SessionStore(tmp_path / "session"),
            transcript=fixture_session([T.user("lane brief")]),
            settings=None,
            root_transcript=fixture_session([old, reply]),
            root_path=root,
        ),
    )
    ctx = Context.of(evt, "test.write", Proposal({"channel": "C1"}, {"text": "reply"}))
    assert reads == []
    assert ctx.answers == ()
    assert len(reads) == 1
    revoked = "C1 do not send the reply"
    call = T.tool("Bash", id="after-answers", command="echo C1")
    appended = [T.user(revoked, uuid="root-revocation"), T.assistant(call), T.user(T.result("observed", of=call))]
    with root.open("ab") as output:
        for index, message in enumerate(appended, len(messages)):
            output.write(json.dumps(fixture_line(index, message)).encode() + b"\n")
    assert revoked in [item.quote for item in ctx.said]
    assert len(reads) == 2
    assert [(touch.tool, touch.input, touch.result) for touch in ctx.touches] == [
        ("Bash", {"command": "echo C1"}, "observed")
    ]
    assert len(reads) == 3
    assert [item["bytes"] for item in reads] == [ROOT_EXCERPT_BYTES + 1] * 3
    fresh = Context.of(evt, "test.write", Proposal({"channel": "C1"}, {"text": "reply"}))
    assert fresh.sources is not ctx.sources
    assert len(reads) == 3
    assert revoked in [item.quote for item in fresh.said]
    assert len(reads) == 4
    assert reads[-1]["bytes"] == ROOT_EXCERPT_BYTES + 1
    print(json.dumps({"root_reads_one_context": 3, "read_bytes": reads, "between_view_revocation_observed": True}))
