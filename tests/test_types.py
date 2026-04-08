from __future__ import annotations

import dataclasses
import re
from typing import get_args

import pytest

from captain_hook.types import (
    Action,
    Agent,
    Command,
    Content,
    CustomCondition,
    Event,
    FilePath,
    HookResult,
    HookSpec,
    InPlanMode,
    RanCommand,
    ReadFile,
    RegisteredHook,
    TCondition,
    TestFile,
    Tool,
    TouchedFile,
    UsedSkill,
    tokens_to_regex,
)


class TestEventFlag:
    def test_event_class_maps_each_member_to_event_dataclass(self) -> None:
        from captain_hook.events import (
            NotificationEvent,
            PostToolUseEvent,
            PostToolUseFailureEvent,
            PreCompactEvent,
            PreToolUseEvent,
            StopEvent,
            SubagentStartEvent,
            SubagentStopEvent,
            UserPromptSubmitEvent,
        )

        mapping = {
            Event.PreToolUse: PreToolUseEvent,
            Event.PostToolUse: PostToolUseEvent,
            Event.PostToolUseFailure: PostToolUseFailureEvent,
            Event.UserPromptSubmit: UserPromptSubmitEvent,
            Event.Stop: StopEvent,
            Event.SubagentStop: SubagentStopEvent,
            Event.SubagentStart: SubagentStartEvent,
            Event.PreCompact: PreCompactEvent,
            Event.Notification: NotificationEvent,
        }
        assert len(mapping) == 9
        for member, expected_cls in mapping.items():
            assert member.event_class is expected_cls

    def test_event_class_raises_on_composite(self) -> None:
        with pytest.raises(ValueError, match="single-member"):
            (Event.PreToolUse | Event.Stop).event_class

    def test_event_class_raises_on_zero(self) -> None:
        with pytest.raises(ValueError):
            Event(0).event_class

    def test_flag_composition_and_containment(self) -> None:
        combo = Event.PreToolUse | Event.PostToolUse
        assert list(combo) == sorted(combo, key=lambda m: m.value)
        assert len(list(combo)) == 2
        seen = set()
        for m in Event:
            assert m.value > 0 and (m.value & (m.value - 1)) == 0
            seen.add(m.value)
        assert len(seen) == 9


class TestAction:
    def test_action_string_formatting(self) -> None:
        result = {Action.block: 1, Action.warn: 2, Action.allow: 3}
        assert result[Action.block] == 1
        assert result[Action("warn")] == 2
        assert len(result) == 3
        assert sorted(a.value for a in Action) == ["allow", "block", "warn"]


class TestHookResult:
    def test_stores_action_and_message(self) -> None:
        r = HookResult(action=Action.warn, message="caution")
        assert r.action == Action.warn
        assert r.message == "caution"

    def test_default_message_is_none(self) -> None:
        assert HookResult(action=Action.allow).message is None

    def test_frozen_rejects_mutation(self) -> None:
        r = HookResult(action=Action.allow)
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.action = Action.block  # type: ignore[misc]


class TestHookSpec:
    def test_minimal_construction_defaults(self) -> None:
        s = HookSpec(events=Event.PreToolUse)
        assert s.events == Event.PreToolUse
        assert s.only_if == ()
        assert s.skip_if == ()
        assert s.message is None
        assert s.block is False
        assert s.respect_gitignore is True
        assert s.max_fires is None
        assert s.async_ is False
        assert s.tests is None

    def test_tests_field_exists(self) -> None:
        assert "tests" in {f.name for f in dataclasses.fields(HookSpec)}

    def test_frozen_rejects_mutation(self) -> None:
        s = HookSpec(events=Event.PreToolUse)
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.block = True  # type: ignore[misc]

    def test_full_construction_stores_all_fields(self) -> None:
        s = HookSpec(
            events=Event.PreToolUse | Event.PostToolUse,
            only_if=(Tool(pattern="Bash"),),
            skip_if=(TestFile(),),
            message="warning",
            block=True,
            respect_gitignore=False,
            max_fires=3,
            async_=True,
        )
        assert Event.PreToolUse in s.events and Event.PostToolUse in s.events
        assert len(s.only_if) == 1
        assert s.message == "warning"
        assert s.block is True
        assert s.respect_gitignore is False
        assert s.max_fires == 3
        assert s.async_ is True


class TestRegisteredHook:
    def test_defaults(self) -> None:
        spec = HookSpec(events=Event.PreToolUse)
        rh = RegisteredHook(spec=spec)
        assert rh.spec is spec
        assert rh.handler is None
        assert rh.name == ""
        assert rh.source_file == ""

    def test_with_handler_stores_callable(self) -> None:
        handler = lambda evt: None  # noqa: E731
        rh = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse),
            handler=handler,
            name="my_hook",
            source_file="hooks.py",
        )
        assert rh.handler is handler
        assert rh.name == "my_hook"

    def test_frozen_rejects_mutation(self) -> None:
        rh = RegisteredHook(spec=HookSpec(events=Event.PreToolUse))
        with pytest.raises(dataclasses.FrozenInstanceError):
            rh.name = "x"  # type: ignore[misc]


class TestTCondition:
    def test_union_contains_exactly_twelve_types(self) -> None:
        args = get_args(TCondition)
        assert len(args) == 12
        expected = {
            Tool,
            FilePath,
            Command,
            Content,
            Agent,
            UsedSkill,
            ReadFile,
            TestFile,
            TouchedFile,
            RanCommand,
            InPlanMode,
            CustomCondition,
        }
        assert set(args) == expected

    def test_tool_stores_pattern(self) -> None:
        assert Tool(pattern="Edit|Write").pattern == "Edit|Write"

    def test_filepath_accepts_varargs(self) -> None:
        assert FilePath("*.py", "*.ts").patterns == ("*.py", "*.ts")

    def test_read_file_accepts_varargs(self) -> None:
        assert ReadFile("A.md", "docs/").patterns == ("A.md", "docs/")

    def test_touched_file_accepts_varargs(self) -> None:
        assert TouchedFile("**/www/**").patterns == ("**/www/**",)

    def test_conditions_are_frozen(self) -> None:
        t = Tool(pattern="Bash")
        with pytest.raises(dataclasses.FrozenInstanceError):
            t.pattern = "Edit"  # type: ignore[misc]
        fp = FilePath("*.py")
        with pytest.raises(dataclasses.FrozenInstanceError):
            fp.patterns = ("*.rs",)  # type: ignore[misc]


class TestTokensToRegex:
    def test_simple_tokens_match(self) -> None:
        pat = tokens_to_regex(["git", "stash"])
        assert re.match(pat, "git stash pop")
        assert re.match(pat, "git  stash")

    def test_wildcard_matches_any_word(self) -> None:
        pat = tokens_to_regex(["git", "*"])
        assert re.match(pat, "git push")
        assert re.match(pat, "git stash")

    def test_pipe_alternatives_match(self) -> None:
        pat = tokens_to_regex(["git", "commit|split"])
        assert re.match(pat, "git commit -m x")
        assert re.match(pat, "git split")
        assert not re.match(pat, "git push")

    def test_empty_list_returns_empty_string(self) -> None:
        assert tokens_to_regex([]) == ""
