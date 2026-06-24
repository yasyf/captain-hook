from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from cc_transcript.activity import SessionActivity, native_user_classifier
from cc_transcript.ids import SessionId
from cc_transcript.parser import parse_event
from cc_transcript.query import Session

from captain_hook.testing.helpers import fixture_session
from captain_hook.tests.helpers import raw_text

if TYPE_CHECKING:
    from cc_transcript.models import UserEvent


def user_event(text: str, **extra: Any) -> UserEvent:
    return parse_event(raw_text("user", text) | extra)


def events_from(*lines: dict[str, Any]) -> list[Any]:
    return [parse_event(line) for line in lines]


class TestConductorDetect:
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            pytest.param({"cwd": "/Users/yasyf/conductor/workspaces/bioqa/test"}, True, id="detects_via_cwd"),
            pytest.param({"cwd": "/Users/yasyf/projects/something"}, False, id="no_detect_wrong_cwd"),
            pytest.param(
                {"transcript_path": "/home/user/.claude/conductor-workspaces/sessions/abc.jsonl"},
                True,
                id="detects_via_transcript_path",
            ),
            pytest.param(
                {"transcript_path": "/home/user/.claude/sessions/abc.jsonl"},
                False,
                id="no_detect_wrong_transcript_path",
            ),
            pytest.param(
                {
                    "events": events_from(
                        raw_text("user", "<system_instruction>You are a helpful agent."),
                        *(raw_text("assistant", f"response {i}") for i in range(10)),
                    )
                },
                True,
                id="detects_via_system_instruction",
            ),
            pytest.param(
                {"events": events_from(raw_text("user", "Hello world"))},
                False,
                id="no_detect_without_system_instruction",
            ),
            pytest.param({}, False, id="no_detect_all_none"),
        ],
    )
    def test_detect(self, kwargs: dict[str, Any], expected: bool):
        from captain_hook.classifiers.conductor import detect

        assert detect(**kwargs) is expected


class TestConductorClassifier:
    @pytest.mark.parametrize(
        ("text", "extra", "expected"),
        [
            pytest.param("Help me fix this bug", {}, True, id="real_user_prompt"),
            pytest.param("   ", {}, False, id="rejects_empty_user_events"),
            pytest.param("Hello", {"isMeta": True}, False, id="rejects_meta_user_events"),
        ],
    )
    def test_classifier(self, text: str, extra: dict[str, Any], expected: bool):
        from captain_hook.classifiers.conductor import classifier

        assert classifier(user_event(text, **extra)) is expected

    def test_all_four_prefixes_filtered(self):
        from captain_hook.classifiers.conductor import classifier

        prefixes = [
            "<system_instruction>",
            "<task-notification>",
            "<local-command-caveat>",
            "<command-name>",
        ]
        for prefix in prefixes:
            assert not classifier(user_event(f"{prefix}payload")), f"Failed to filter prefix: {prefix}"


class TestDroidDetect:
    def test_detects_via_env_var(self):
        from captain_hook.classifiers.droid import detect

        with patch.dict(os.environ, {"FACTORY_PROJECT_DIR": "/tmp/project"}):
            assert detect()

    def test_no_detect_without_env_var(self):
        from captain_hook.classifiers.droid import detect

        env = dict(os.environ)
        env.pop("FACTORY_PROJECT_DIR", None)
        with patch.dict(os.environ, env, clear=True):
            assert not detect()


class TestDroidClassifier:
    def test_is_native_classifier(self):
        from captain_hook.classifiers.droid import classifier

        assert classifier is native_user_classifier

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("Hello", True, id="user_prompt"),
            pytest.param("", False, id="rejects_empty_user_events"),
        ],
    )
    def test_classifier(self, text: str, expected: bool):
        from captain_hook.classifiers.droid import classifier

        assert classifier(user_event(text)) is expected


class TestNativeDetect:
    def test_always_detects(self):
        from captain_hook.classifiers.native import detect

        assert detect()
        assert detect(cwd="/anything")
        assert detect(transcript_path="/anything")
        assert detect(events=[])


class TestNativeClassifier:
    def test_is_native_classifier(self):
        from captain_hook.classifiers.native import classifier

        assert classifier is native_user_classifier

    @pytest.mark.parametrize(
        ("text", "extra", "expected"),
        [
            pytest.param("Hello", {}, True, id="user_prompt"),
            pytest.param("Hello", {"isSidechain": True}, False, id="rejects_sidechain_user_events"),
        ],
    )
    def test_classifier(self, text: str, extra: dict[str, Any], expected: bool):
        from captain_hook.classifiers.native import classifier

        assert classifier(user_event(text, **extra)) is expected


class TestDetectPriorityChain:
    def test_droid_wins_when_env_set(self):
        from captain_hook.classifiers import detect
        from captain_hook.classifiers.droid import classifier as droid_classifier

        with patch.dict(os.environ, {"FACTORY_PROJECT_DIR": "/tmp/project"}):
            assert detect(cwd="/Users/yasyf/conductor/workspaces/bioqa/test") is droid_classifier

    def test_conductor_when_path_matches(self):
        from captain_hook.classifiers import detect
        from captain_hook.classifiers.conductor import classifier as conductor_classifier

        env = dict(os.environ)
        env.pop("FACTORY_PROJECT_DIR", None)
        with patch.dict(os.environ, env, clear=True):
            assert detect(cwd="/Users/yasyf/conductor/workspaces/bioqa/test") is conductor_classifier

    def test_native_as_fallback(self):
        from captain_hook.classifiers import detect
        from captain_hook.classifiers.native import classifier as native_classifier

        env = dict(os.environ)
        env.pop("FACTORY_PROJECT_DIR", None)
        with patch.dict(os.environ, env, clear=True):
            assert detect(cwd="/Users/yasyf/projects/something") is native_classifier

    def test_conductor_filters_synthetic_in_chain(self):
        from captain_hook.classifiers import detect

        env = dict(os.environ)
        env.pop("FACTORY_PROJECT_DIR", None)
        with patch.dict(os.environ, env, clear=True):
            result = detect(cwd="/Users/yasyf/conductor/workspaces/bioqa/test")
            assert not result(user_event("<system_instruction>test"))

    def test_classifier_returns_correct_callable(self):
        from captain_hook.classifiers import detect

        assert detect()(user_event("Hello world"))


class TestClassifierSegmentsSession:
    def test_conductor_classifier_filters_synthetic_prompts(self):
        from captain_hook.classifiers.conductor import classifier

        events = events_from(
            raw_text("user", "<system_instruction>You are helpful"),
            raw_text("user", "Fix the bug"),
            raw_text("assistant", "Sure, I can help"),
        )
        session = Session.from_activity(
            SessionActivity.from_events(SessionId("sess"), events, user_classifier=classifier)
        )
        assert session.first_prompt == "Fix the bug"
        assert session.user_said("fix the bug")
        assert not session.user_said("system_instruction")

    def test_fixture_session_auto_detects_conductor(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("FACTORY_PROJECT_DIR", raising=False)
        session = fixture_session(
            [
                raw_text("user", "<system_instruction>setup"),
                raw_text("user", "Real user message"),
            ]
        )
        assert session.first_prompt == "Real user message"

    def test_fixture_session_native_keeps_all_prompts(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("FACTORY_PROJECT_DIR", raising=False)
        session = fixture_session(
            [
                raw_text("user", "normal message"),
                raw_text("user", "another message"),
            ]
        )
        assert session.first_prompt == "normal message"
        assert len(session.turns) == 2
