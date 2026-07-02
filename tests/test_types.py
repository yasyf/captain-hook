from __future__ import annotations

import pytest

from captain_hook.types import (
    Event,
    HookSpec,
    TestFile,
    Tool,
)


class TestEventFlag:
    def test_event_class_maps_each_member_to_event_dataclass(self) -> None:
        from captain_hook.events import (
            NotificationEvent,
            PostToolUseEvent,
            PostToolUseFailureEvent,
            PreCompactEvent,
            PreToolUseEvent,
            SessionEndEvent,
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
            Event.SessionEnd: SessionEndEvent,
        }
        assert len(mapping) == 10
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
        assert len(seen) == 10


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

    def test_full_construction_stores_all_fields(self) -> None:
        s = HookSpec(
            events=Event.PreToolUse | Event.PostToolUse,
            only_if=(Tool("Bash"),),
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
