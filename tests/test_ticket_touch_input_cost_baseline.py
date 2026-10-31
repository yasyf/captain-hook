from __future__ import annotations

import json

import pytest

from captain_hook import T
from captain_hook.snapshots.client import CURRENT_CLIENT
from captain_hook.testing.helpers import fixture_line
from captain_hook.testing.snapshots import FixtureOwner
from captain_hook.tickets.context import touch_of


@pytest.mark.parametrize("unused_bytes", [32, 2 * 1024 * 1024])
def test_existing_touch_projection_transfers_unused_result_content(tmp_path, monkeypatch, unused_bytes):
    command = T.tool("Bash", id="touch-cost", command="echo C1")
    messages = [
        T.user("inspect C1"),
        T.assistant(command),
        T.user(T.result("x" * unused_bytes, of=command), toolUseResult={"marker": "small"}),
    ]
    path = tmp_path / "acting.jsonl"
    path.write_text("".join(
        json.dumps(fixture_line(index, message), ensure_ascii=False) + "\n"
        for index, message in enumerate(messages)
    ))
    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    exchange = fixture.client._exchange
    projections = []

    def recorded(wrapper):
        result = exchange(wrapper)
        response = result["response"]
        if wrapper["request"]["operation"] == "query" and response["status"] == "ok":
            projections.append(response)
        return result

    monkeypatch.setattr(fixture.client, "_exchange", recorded)
    session = fixture.load(path)
    try:
        uses = session.recent_messages(60).tool_calls.with_errors.items
        assert len(uses) == 1
        assert uses[0].result.content == "x" * unused_bytes
        assert uses[0].result.tool_use_result == {"marker": "small"}
        touch = touch_of(uses[0])
        assert touch.tool == "Bash"
        assert touch.input == {"command": "echo C1"}
        assert touch.result == '{"marker": "small"}'
        records = [record for response in projections for record in response["data"].get("records_json", [])]
        wire_bytes = sum(len(record.encode()) for record in records)
        assert wire_bytes >= unused_bytes
        assert len(touch.text.encode()) < 128
        print(json.dumps({
            "unused_result_content_bytes": unused_bytes,
            "actual_native_record_bytes": wire_bytes,
            "actual_touch_text_bytes": len(touch.text.encode()),
            "query_responses": len(projections),
        }))
    finally:
        session.release()
        CURRENT_CLIENT.reset(token)
        fixture.close()
