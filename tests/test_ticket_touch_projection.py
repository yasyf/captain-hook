from __future__ import annotations

import json

import pytest

from captain_hook import T
from captain_hook.context import HookContext
from captain_hook.events import PreToolUseEvent
from captain_hook.session import SessionStore
from captain_hook.snapshots.client import CURRENT_CLIENT
from captain_hook.testing.helpers import fixture_line, fixture_session
from captain_hook.testing.snapshots import FixtureOwner
from captain_hook.tickets.context import QUESTION_TOOL, TranscriptSources, ordered_uses, touch_of


def event(tmp_path, transcript):
    return PreToolUseEvent(
        _raw={"session_id": "touch-fixture", "tool_name": "Bash", "tool_use_id": "current", "tool_input": {}},
        ctx=HookContext(session=SessionStore(tmp_path), transcript=transcript, settings=None),
    )


@pytest.mark.parametrize("unrelated", [32, 4096])
@pytest.mark.parametrize("window", [8, 20_000])
def test_touches_match_full_turn_oracle_without_projecting_assistant_text(tmp_path, monkeypatch, unrelated, window):
    messages = [T.user("start")]
    for index in range(unrelated):
        messages.append(T.assistant(f"unrelated-{index}:" + "x" * 512))
    first = T.tool("Bash", id="duplicate", command="old")
    second = T.tool("Bash", id="other", command="error")
    last = T.tool("Bash", id="duplicate", command="replacement")
    question = T.tool("AskUserQuestion", id="question", questions=[])
    pending = T.tool("Bash", id="pending", command="pending")
    messages += [
        T.assistant(first), T.user(T.result("old", of=first), timestamp="2026-01-01T00:00:01Z"),
        T.assistant(second), T.user(T.result("failed", of=second, is_error=True), timestamp="2026-01-01T00:00:02Z"),
        T.assistant(last), T.user(T.result("new", of=last), timestamp="2026-01-01T00:00:02Z"),
        T.assistant(question), T.user(T.result("answer", of=question)), T.assistant(pending),
    ]
    local = fixture_session(messages)
    expected = [
        touch_of(use) for use in ordered_uses(local.recent_messages(window).turns)
        if use.call.name != QUESTION_TOOL
    ]
    assert TranscriptSources(event(tmp_path, local), (), window).touches() == expected
    path = tmp_path / "touch.jsonl"
    path.write_text("".join(json.dumps(fixture_line(index, message)) + "\n" for index, message in enumerate(messages)))
    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    native = fixture.load(path)
    requests = []
    output_bytes = []
    exchange = fixture.client._exchange

    def measured(wrapper):
        result = exchange(wrapper)
        if wrapper["request"]["operation"] == "query":
            requests.append(wrapper["request"]["query"])
            output_bytes.append(len(json.dumps(result).encode()))
        return result

    monkeypatch.setattr(fixture.client, "_exchange", measured)
    try:
        provider = TranscriptSources(event(tmp_path, native), (), window)
        old = [touch_of(use) for use in ordered_uses(turn for turn, _ in provider.turns(window)) if use.call.name != QUESTION_TOOL]
        assert old == expected
        full_bytes = sum(output_bytes)
        requests.clear()
        output_bytes.clear()
        assert provider.touches() == expected
        assert requests and all(query["kind"] == "tool_calls" for query in requests)
        projected_bytes = sum(output_bytes)
        if window == 20_000:
            assert projected_bytes * 2 < full_bytes
            assert projected_bytes < 32_000
            assert any(touch.input.get("command") == "error" for touch in expected)
            assert any(touch.input.get("command") == "pending" for touch in expected)
            assert not any(touch.input.get("command") == "old" for touch in expected)
        print(f"ticket_touches unrelated={unrelated} window={window} full_bytes={full_bytes} tool_bytes={projected_bytes}")
    finally:
        native.release()
        CURRENT_CLIENT.reset(token)
        fixture.close()


def test_touch_projection_preserves_source_winners_and_timestamp_ties(tmp_path, monkeypatch):
    duplicate = T.tool("Bash", id="duplicate", command="old")
    other = T.tool("Bash", id="other", command="middle")
    replacement = T.tool("Bash", id="duplicate", command="new")
    first = fixture_session([
        T.user("root"), T.assistant(duplicate),
        T.user(T.result("old", of=duplicate), timestamp="2026-01-01T00:00:01Z"),
        T.assistant(other), T.user(T.result("middle", of=other), timestamp="2026-01-01T00:00:02Z"),
    ])
    second = fixture_session([
        T.user("agent"), T.assistant(replacement),
        T.user(T.result("new", of=replacement), timestamp="2026-01-01T00:00:02Z"),
    ])
    monkeypatch.setattr(TranscriptSources, "sessions", lambda self: [(first, True), (second, False)])
    provider = TranscriptSources(event(tmp_path, first), ())
    expected = [touch_of(use) for use in ordered_uses([*first.turns, *second.turns])]
    assert provider.touches() == expected
    assert [touch.input["command"] for touch in expected] == ["new", "middle"]
