import re
from dataclasses import replace

import pytest

from captain_hook.testing.helpers import fixture_session
from captain_hook.testing.snapshots import FixtureOwner
from tests.helpers import raw_assistant, raw_text, raw_tool_result, raw_tool_use
from tests.test_snapshot_fixture_owner import write_messages


@pytest.mark.parametrize("current_turn", [False, True], ids=["session", "current-turn"])
@pytest.mark.parametrize(
    ("errors", "name", "pattern", "expected"),
    [
        ("exclude", None, None, ("old", "done", "pending", "question", "read")),
        ("include", None, None, ("old", "done", "error", "pending", "question", "read")),
        ("only", None, None, ("error",)),
        ("exclude", "Bash", None, ("old", "done", "pending")),
        ("exclude", "Bash", re.compile("KEEP", re.IGNORECASE), ("done", "pending")),
        ("only", "Bash", re.compile("keep"), ("error",)),
        ("exclude", "AskUserQuestion", None, ("question",)),
    ],
    ids=["default", "include-errors", "only-errors", "named", "regex-flags", "failed-regex", "human-pending"],
)
def test_remote_toolcall_items_match_native_filtered_view(
    tmp_path, monkeypatch, current_turn, errors, name, pattern, expected
):
    messages = [
        raw_text("user", "old prompt"),
        raw_assistant(raw_tool_use("Bash", {"command": "echo old"}, "old")),
        raw_tool_result("old"),
        raw_text("user", "current prompt"),
        raw_assistant(raw_tool_use("Bash", {"command": "echo keep"}, "done")),
        raw_tool_result("done"),
        raw_assistant(raw_tool_use("Bash", {"command": "echo keep error"}, "error")),
        raw_tool_result("error", "failed", is_error=True),
        raw_assistant(raw_tool_use("Bash", {"command": "echo KEEP pending"}, "pending")),
        raw_assistant(raw_tool_use("AskUserQuestion", {"questions": []}, "question")),
        raw_assistant(raw_tool_use("Read", {"file_path": "/tmp/other"}, "read")),
    ]
    source = tmp_path / "fixture.jsonl"
    write_messages(source, *messages)
    local = fixture_session(messages, path=source)
    fixture = FixtureOwner()
    remote = fixture.load(source)
    try:
        local_view = local.current_turn if current_turn else local
        remote_view = remote.current_turn if current_turn else remote
        native = local_view.tool_calls
        query = replace(remote_view.tool_calls, errors=errors)
        if errors == "include":
            native = native.with_errors
        elif errors == "only":
            native = native.failed()
        if name is not None:
            native = native.named(name)
            query = query.named(name)
        if pattern is not None:
            native = native.where_input(command=pattern)
            query = query.where_input(command=pattern)
        requests = []
        exchange = fixture.client._exchange
        expected_view = remote_view.view()

        def named_projection_only(wrapper):
            request = wrapper["request"]
            if request["operation"] == "query":
                requests.append(request)
                assert request["query"] == {"kind": "tool_calls", "name": name, "order": "forward"}
                assert request["view"] == expected_view
            return exchange(wrapper)

        monkeypatch.setattr(fixture.client, "_exchange", named_projection_only)
        items = query.items
        expected_ids = tuple(identifier for identifier in expected if not current_turn or identifier != "old")
        assert tuple(use.ref.tool_use_id for use in items) == expected_ids
        assert tuple(use.ref.tool_use_id for use in native.items) == expected_ids
        assert tuple((use.call.raw, None if use.result is None else use.result.is_error) for use in items) == tuple(
            (use.call.raw, None if use.result is None else use.result.is_error) for use in native.items
        )
        assert isinstance(items, tuple)
        assert len(requests) == 1
    finally:
        remote.release()
        fixture.close()
