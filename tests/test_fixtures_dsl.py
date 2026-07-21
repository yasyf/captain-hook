from __future__ import annotations

import re

import pytest
from cc_transcript.models import AssistantEvent, UserEvent

from captain_hook.testing.fixtures import T
from captain_hook.testing.helpers import fixture_session

TU_ID = re.compile(r"^tu-\d+$")


class TestBuilderShapes:
    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            pytest.param(
                T.user("Re-enter plan mode, don't do any more work."),
                {
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": "Re-enter plan mode, don't do any more work."}]},
                },
                id="user_text",
            ),
            pytest.param(
                T.user({"type": "text", "text": "raw block"}, isMeta=True),
                {
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": "raw block"}]},
                    "isMeta": True,
                },
                id="user_dict_block_meta",
            ),
            pytest.param(
                T.assistant("planning the change"),
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "planning the change"}]},
                },
                id="assistant_text",
            ),
            pytest.param(
                T.assistant(T.tool("EnterPlanMode", id="tu-plan")),
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "id": "tu-plan", "name": "EnterPlanMode", "input": {}}]
                    },
                },
                id="assistant_tool_block",
            ),
            pytest.param(
                T.assistant(T.thinking("The user asked for a rename only.")),
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "thinking", "thinking": "The user asked for a rename only."}]},
                },
                id="assistant_thinking_block",
            ),
            pytest.param(
                T.tool("Bash", id="tu-fixed", command="uv run pytest"),
                {"type": "tool_use", "id": "tu-fixed", "name": "Bash", "input": {"command": "uv run pytest"}},
                id="tool_explicit_id",
            ),
            pytest.param(
                T.result("done", of=T.tool("Grep", id="tu-grep", pattern="TODO")),
                {"type": "tool_result", "tool_use_id": "tu-grep", "content": "done", "is_error": False},
                id="result_of_block",
            ),
            pytest.param(
                T.result("boom", of="tu-raw", is_error=True),
                {"type": "tool_result", "tool_use_id": "tu-raw", "content": "boom", "is_error": True},
                id="result_of_raw_str",
            ),
        ],
    )
    def test_golden_shape(self, block: dict[str, object], expected: dict[str, object]) -> None:
        assert block == expected

    def test_tool_auto_id(self) -> None:
        block = T.tool("EnterPlanMode")
        assert TU_ID.match(block.pop("id"))
        assert block == {"type": "tool_use", "name": "EnterPlanMode", "input": {}}

    def test_result_unpaired_auto_id(self) -> None:
        block = T.result()
        assert TU_ID.match(block.pop("tool_use_id"))
        assert block == {"type": "tool_result", "content": "ok", "is_error": False}

    def test_tool_turn_id_correlation(self) -> None:
        call_line, result_line = T.tool_turn("Bash", result="boom", is_error=True, command="uv run pytest")
        use_block = call_line["message"]["content"][0]
        result_block = result_line["message"]["content"][0]
        assert call_line["type"] == "assistant"
        assert result_line["type"] == "user"
        assert use_block == {
            "type": "tool_use",
            "id": use_block["id"],
            "name": "Bash",
            "input": {"command": "uv run pytest"},
        }
        assert TU_ID.match(use_block["id"])
        assert result_block == {
            "type": "tool_result",
            "tool_use_id": use_block["id"],
            "content": "boom",
            "is_error": True,
        }

    def test_meta_merges_onto_line(self) -> None:
        line = T.assistant("thinking", isMeta=True, uuid="a-1")
        assert line["isMeta"] is True
        assert line["uuid"] == "a-1"
        assert line["message"] == {"content": [{"type": "text", "text": "thinking"}]}

    def test_result_rejects_a_non_tool_dict(self) -> None:
        with pytest.raises(TypeError, match="tool_use block"):
            T.result("x", of={"foo": "bar"})

    def test_testing_namespace_lists_t(self) -> None:
        import captain_hook.testing as testing

        assert "T" in dir(testing)


class TestSessionRoundTrip:
    def test_user_and_assistant_text_lift(self) -> None:
        session = fixture_session([T.user("hello there"), T.assistant("working on it")])
        user = next(e for e in session.events if isinstance(e, UserEvent))
        assistant = next(e for e in session.events if isinstance(e, AssistantEvent))
        assert user.text == "hello there"
        assert assistant.text == "working on it"

    def test_tool_block_lifts_with_intact_input(self) -> None:
        session = fixture_session([T.user("go"), T.assistant(T.tool("Bash", id="tu-c", command="uv run pytest"))])
        call = list(session.tool_calls)[0]
        assert call.call.name == "Bash"
        assert call.call.command == "uv run pytest"

    def test_tool_turn_result_joins_call(self) -> None:
        session = fixture_session([T.user("go"), *T.tool_turn("Grep", result="3 matches", pattern="TODO")])
        call = list(session.tool_calls)[0]
        assert call.call.name == "Grep"
        assert call.result is not None
        assert call.result.content == "3 matches"
        assert call.result.is_error is False

    def test_error_result_lifts_to_failure(self) -> None:
        session = fixture_session(
            [T.user("go"), *T.tool_turn("Bash", result="ModuleNotFoundError", is_error=True, command="uv run pytest")]
        )
        assert session.tool_calls.count() == 0
        failed = list(session.tool_calls.failed())
        assert len(failed) == 1
        assert failed[0].result is not None
        assert failed[0].result.content == "ModuleNotFoundError"
        assert failed[0].result.is_error is True
        assert session.count_failures() == 1

    def test_ismeta_lifts_to_event_meta(self) -> None:
        session = fixture_session([T.user("normal turn"), T.user("meta turn", isMeta=True)])
        users = [e for e in session.events if isinstance(e, UserEvent)]
        assert users[0].meta.is_meta is False
        assert users[1].meta.is_meta is True

    def test_raw_dicts_and_dsl_mix_in_one_transcript(self) -> None:
        session = fixture_session(
            [
                {"type": "user", "message": {"content": "raw hello"}},
                T.assistant(T.tool("Bash", id="tu-m", command="ls")),
                T.user(T.result("ok", of="tu-m")),
            ]
        )
        assert any(isinstance(e, UserEvent) and e.text == "raw hello" for e in session.events)
        call = list(session.tool_calls)[0]
        assert call.call.name == "Bash"
        assert call.call.command == "ls"
        assert call.result is not None
        assert call.result.content == "ok"
