import json

import pytest

from captain_hook.conditions import ran_any_command
from captain_hook.testing.snapshots import FixtureOwner
from captain_hook.types import Regex
from tests.helpers import raw_assistant, raw_text, raw_tool_use


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
