import pytest

from captain_hook.snapshots.client import GraphSources
from captain_hook.testing.snapshots import FixtureOwner
from tests.helpers import raw_assistant, raw_text, raw_tool_use
from tests.test_snapshot_fixture_owner import write_messages


@pytest.fixture
def registered_foreground(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = tmp_path / "root.jsonl"
    member = tmp_path / "thread-a.jsonl"
    unrelated = tmp_path / "thread-unregistered.jsonl"
    write_messages(root, raw_text("user", "root"))
    write_messages(member, raw_text("user", "registered"), raw_assistant(raw_tool_use("Read", {"file_path": "before.md"})))
    write_messages(unrelated, raw_text("user", "unregistered"), raw_assistant(raw_tool_use("Grep", {"pattern": "unused"})))
    fixture = FixtureOwner()
    fixture.client.bind_tool_registry({})
    fixture.client._root_warm_scheduler = lambda *_: pytest.fail("foreground evidence scheduled speculative warming")
    operations = []
    exchange = fixture.client._exchange

    def record(wrapper):
        operations.append(wrapper["request"]["operation"])
        return exchange(wrapper)

    fixture.client._exchange = record
    try:
        yield fixture, root, member, GraphSources(thread_ids=("thread-a",), roots=(tmp_path,)), operations
    finally:
        fixture.close()


def test_cold_registered_foreground_evidence_needs_no_bulk_warm(registered_foreground):
    fixture, root, _, sources, operations = registered_foreground
    session = fixture.load(root).with_registered_sources(sources)
    try:
        assert session.has_tool("Read") is True
        assert session.has_tool("Grep") is False
        assert "prepare_graph" in operations
        assert "query_graph" in operations
        assert "warm_registered" not in operations
        counters = fixture.client.call("stats")["data"]["counters"]
        assert counters["cold_parses"] == 2
    finally:
        session.release()


def test_registered_foreground_evidence_revalidates_changes_without_bulk_warm(registered_foreground):
    fixture, root, member, sources, operations = registered_foreground
    original = fixture.load(root).with_registered_sources(sources)
    try:
        assert original.has_tool("Read") is True
    finally:
        original.release()

    write_messages(member, raw_text("user", "changed"), raw_assistant(raw_tool_use("Grep", {"pattern": "new"})))
    changed = fixture.load(root).with_registered_sources(sources)
    try:
        assert changed.has_tool("Grep") is True
        assert changed.has_tool("Read") is False
    finally:
        changed.release()

    children = member.with_suffix("") / "subagents"
    children.mkdir(parents=True)
    write_messages(
        children / "agent-new.jsonl",
        raw_text("user", "child"),
        raw_assistant(raw_tool_use("Bash", {"command": "pwd"})),
    )
    expanded = fixture.load(root).with_registered_sources(sources)
    try:
        assert expanded.has_tool("Bash") is True
        assert "warm_registered" not in operations
    finally:
        expanded.release()
