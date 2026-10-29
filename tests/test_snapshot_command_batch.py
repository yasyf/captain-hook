import json

import pytest

from captain_hook.conditions import ran_any_command
from captain_hook.snapshots.client import EvidenceIncomplete, GraphSources
from captain_hook.testing.snapshots import FixtureOwner
from captain_hook.types import Regex
from tests.helpers import raw_assistant, raw_text, raw_tool_use
from tests.test_snapshot_existential import cursor_releases, inputs_page, paged_session
from tests.test_snapshot_fixture_owner import write_messages


@pytest.mark.parametrize("subagents", [False, True])
def test_command_spellings_share_one_native_projection(tmp_path, subagents):
    source = tmp_path / "session.jsonl"
    source.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [
                raw_text("user", "go"),
                raw_assistant(raw_tool_use("Bash", {"command": "cat <<'EOF'\nuvx pyright src\nEOF"}, "parent")),
            ]
        )
    )
    children = tmp_path / "session" / "subagents"
    children.mkdir(parents=True)
    (children / "agent-child.jsonl").write_text(
        json.dumps(raw_assistant(raw_tool_use("Bash", {"command": "uvx pyright src"}, "child"))) + "\n"
    )
    owner = FixtureOwner()
    try:
        session = owner.load(source)
        operations = []
        exchange = owner.client._exchange

        def record(wrapper):
            request = wrapper["request"]
            if request["operation"] in {"query", "query_graph"}:
                operations.append(request["query"]["kind"])
            return exchange(wrapper)

        owner.client._exchange = record
        assert (
            ran_any_command(
                session,
                [("uv", "run", "ty", "check"), (Regex(r"^uvx pyright\b"),), ("uvx", "pyright")],
                subagents=subagents,
            )
            is subagents
        )
        assert operations == ["deep_predicate_inputs" if subagents else "predicate_inputs"]
    finally:
        owner.close()


@pytest.mark.parametrize("matches", [False, True])
def test_command_batch_stops_after_the_first_matching_native_page(tmp_path, monkeypatch, record_property, matches):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = tmp_path / "root.jsonl"
    write_messages(root, raw_text("user", "delegate"))
    ids = tuple(f"thread-{index:02d}" for index in range(16))
    member_bytes = 0
    for index, thread in enumerate(ids):
        path = tmp_path / f"{thread}.jsonl"
        tool = (
            raw_tool_use("Bash", {"command": "uvx pyright src"})
            if matches and index == 0
            else raw_tool_use("Read", {"file_path": "unused.md"})
        )
        write_messages(path, raw_text("user", "delegate"), raw_assistant(tool))
        assert path.stat().st_size <= 1024
        member_bytes += path.stat().st_size
    fixture = FixtureOwner()
    fixture.client.bind_tool_registry({})
    operations = []
    exchange = fixture.client._exchange

    def record(wrapper):
        operations.append(wrapper["request"])
        return exchange(wrapper)

    fixture.client._exchange = record
    fixture.client._cleanup_exchange = record
    try:
        transcript = fixture.load(root).with_registered_sources(GraphSources(thread_ids=ids, roots=(tmp_path,)))
        try:
            before = fixture.client.call("stats")["data"]["counters"]
            operations.clear()
            assert ran_any_command(
                transcript,
                [("uv", "run", "ty", "check"), (Regex(r"^uvx pyright\b"),), ("uvx", "pyright")],
                subagents=True,
            ) is matches
            after = fixture.client.call("stats")["data"]["counters"]
            metrics = {
                name: after[name] - before[name]
                for name in ("source_opens", "cold_parses", "source_bytes_read", "prepared_cache_bytes_read")
            }
            metrics["query_pages"] = sum(request["operation"] in {"query_graph", "resume"} for request in operations)
            metrics["member_bytes"] = member_bytes
            record_property("native_metrics", json.dumps(metrics, sort_keys=True))
            if matches:
                assert 0 < metrics["source_opens"] < len(ids), metrics
                assert 0 < metrics["cold_parses"] < len(ids), metrics
                assert 0 < metrics["source_bytes_read"] < member_bytes, metrics
                assert metrics["query_pages"] == 1, metrics
                assert len(cursor_releases(operations)) == 1
            else:
                assert metrics["source_opens"] >= len(ids), metrics
                assert metrics["cold_parses"] >= len(ids), metrics
                assert metrics["source_bytes_read"] >= member_bytes, metrics
                assert metrics["query_pages"] >= 2, metrics
            assert not any(request["operation"].startswith("warm_") for request in operations)
        finally:
            transcript.release()
    finally:
        fixture.close()


@pytest.mark.parametrize("status", ["changed", "stale_cursor"])
def test_command_batch_late_failure_cannot_become_a_complete_negative(status):
    with paged_session([[inputs_page("unrelated", cursor="first"), status]]) as (session, operations):
        with pytest.raises(EvidenceIncomplete) as caught:
            ran_any_command(session, [("wanted",)], subagents=True)
        assert caught.value.status == status
        assert sum(request["operation"] == "prepare_graph" for request in operations) == 1
