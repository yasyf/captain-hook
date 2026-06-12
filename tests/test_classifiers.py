from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

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
    def test_detects_via_cwd(self):
        from captain_hook.classifiers.conductor import detect

        assert detect(cwd="/Users/yasyf/conductor/workspaces/bioqa/test")

    def test_no_detect_wrong_cwd(self):
        from captain_hook.classifiers.conductor import detect

        assert not detect(cwd="/Users/yasyf/projects/something")

    def test_detects_via_transcript_path(self):
        from captain_hook.classifiers.conductor import detect

        assert detect(transcript_path="/home/user/.claude/conductor-workspaces/sessions/abc.jsonl")

    def test_no_detect_wrong_transcript_path(self):
        from captain_hook.classifiers.conductor import detect

        assert not detect(transcript_path="/home/user/.claude/sessions/abc.jsonl")

    def test_detects_via_system_instruction(self):
        from captain_hook.classifiers.conductor import detect

        events = events_from(
            raw_text("user", "<system_instruction>You are a helpful agent."),
            *(raw_text("assistant", f"response {i}") for i in range(10)),
        )
        assert detect(events=events)

    def test_no_detect_without_system_instruction(self):
        from captain_hook.classifiers.conductor import detect

        assert not detect(events=events_from(raw_text("user", "Hello world")))

    def test_no_detect_all_none(self):
        from captain_hook.classifiers.conductor import detect

        assert not detect()


class TestConductorClassifier:
    def test_real_user_prompt(self):
        from captain_hook.classifiers.conductor import classifier

        assert classifier(user_event("Help me fix this bug"))

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

    def test_rejects_empty_user_events(self):
        from captain_hook.classifiers.conductor import classifier

        assert not classifier(user_event("   "))

    def test_rejects_meta_user_events(self):
        from captain_hook.classifiers.conductor import classifier

        assert not classifier(user_event("Hello", isMeta=True))


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

    def test_user_prompt(self):
        from captain_hook.classifiers.droid import classifier

        assert classifier(user_event("Hello"))

    def test_rejects_empty_user_events(self):
        from captain_hook.classifiers.droid import classifier

        assert not classifier(user_event(""))


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

    def test_user_prompt(self):
        from captain_hook.classifiers.native import classifier

        assert classifier(user_event("Hello"))

    def test_rejects_sidechain_user_events(self):
        from captain_hook.classifiers.native import classifier

        assert not classifier(user_event("Hello", isSidechain=True))


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
