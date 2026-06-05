from __future__ import annotations

import os
from unittest.mock import patch

from captain_hook.tests.helpers import raw_text, raw_text_block
from captain_hook.transcript.models import TranscriptMessage


def _msg(type: str, text: str) -> TranscriptMessage:
    return TranscriptMessage.from_raw(type=type, content=[raw_text_block(text)], raw={})


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

        msgs = [_msg("user", "<system_instruction>You are a helpful agent.")] + [
            _msg("assistant", f"response {i}") for i in range(10)
        ]
        assert detect(messages=msgs)

    def test_no_detect_without_system_instruction(self):
        from captain_hook.classifiers.conductor import detect

        msgs = [_msg("user", "Hello world")]
        assert not detect(messages=msgs)

    def test_no_detect_all_none(self):
        from captain_hook.classifiers.conductor import detect

        assert not detect()


class TestConductorClassifier:
    def test_real_user_message(self):
        from captain_hook.classifiers.conductor import classifier

        msg = _msg("user", "Help me fix this bug")
        assert classifier(msg)

    def test_all_four_prefixes_filtered(self):
        from captain_hook.classifiers.conductor import classifier

        prefixes = [
            "<system_instruction>",
            "<task-notification>",
            "<local-command-caveat>",
            "<command-name>",
        ]
        for prefix in prefixes:
            msg = _msg("user", f"{prefix}payload")
            assert not classifier(msg), f"Failed to filter prefix: {prefix}"

    def test_rejects_assistant_messages(self):
        from captain_hook.classifiers.conductor import classifier

        msg = _msg("assistant", "I can help with that")
        assert not classifier(msg)

    def test_rejects_empty_user_messages(self):
        from captain_hook.classifiers.conductor import classifier

        msg = _msg("user", "   ")
        assert not classifier(msg)


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
    def test_user_message(self):
        from captain_hook.classifiers.droid import classifier

        msg = _msg("user", "Hello")
        assert classifier(msg)

    def test_rejects_assistant_messages(self):
        from captain_hook.classifiers.droid import classifier

        msg = _msg("assistant", "Hello")
        assert not classifier(msg)

    def test_rejects_empty_user_messages(self):
        from captain_hook.classifiers.droid import classifier

        msg = _msg("user", "")
        assert not classifier(msg)


class TestNativeDetect:
    def test_always_detects(self):
        from captain_hook.classifiers.native import detect

        assert detect()
        assert detect(cwd="/anything")
        assert detect(transcript_path="/anything")
        assert detect(messages=[])


class TestNativeClassifier:
    def test_user_message(self):
        from captain_hook.classifiers.native import classifier

        msg = _msg("user", "Hello")
        assert classifier(msg)

    def test_rejects_assistant(self):
        from captain_hook.classifiers.native import classifier

        msg = _msg("assistant", "Hello")
        assert not classifier(msg)


class TestDetectPriorityChain:
    def test_droid_wins_when_env_set(self):
        from captain_hook.classifiers import detect
        from captain_hook.classifiers.droid import classifier as droid_classifier

        with patch.dict(os.environ, {"FACTORY_PROJECT_DIR": "/tmp/project"}):
            result = detect(cwd="/Users/yasyf/conductor/workspaces/bioqa/test")
            msg = _msg("user", "Hello")
            assert result(msg) == droid_classifier(msg)

    def test_conductor_when_path_matches(self):
        from captain_hook.classifiers import detect
        from captain_hook.classifiers.conductor import classifier as conductor_classifier

        env = dict(os.environ)
        env.pop("FACTORY_PROJECT_DIR", None)
        with patch.dict(os.environ, env, clear=True):
            result = detect(cwd="/Users/yasyf/conductor/workspaces/bioqa/test")
            msg = _msg("user", "Hello")
            assert result(msg) == conductor_classifier(msg)

    def test_native_as_fallback(self):
        from captain_hook.classifiers import detect
        from captain_hook.classifiers.native import classifier as native_classifier

        env = dict(os.environ)
        env.pop("FACTORY_PROJECT_DIR", None)
        with patch.dict(os.environ, env, clear=True):
            result = detect(cwd="/Users/yasyf/projects/something")
            msg = _msg("user", "Hello")
            assert result(msg) == native_classifier(msg)

    def test_conductor_filters_synthetic_in_chain(self):
        from captain_hook.classifiers import detect

        env = dict(os.environ)
        env.pop("FACTORY_PROJECT_DIR", None)
        with patch.dict(os.environ, env, clear=True):
            result = detect(cwd="/Users/yasyf/conductor/workspaces/bioqa/test")
            synthetic = _msg("user", "<system_instruction>test")
            assert not result(synthetic)

    def test_classifier_returns_correct_callable(self):
        from captain_hook.classifiers import detect

        result = detect()
        real = _msg("user", "Hello world")
        assert result(real)


class TestClassifierIntegration:
    def test_conductor_classifier_with_empty_text(self):
        from captain_hook.classifiers.conductor import classifier

        msg = _msg("user", "")
        assert not classifier(msg)

    def test_all_classifiers_accept_real_user_messages(self):
        from captain_hook.classifiers.conductor import classifier as c
        from captain_hook.classifiers.droid import classifier as d
        from captain_hook.classifiers.native import classifier as n

        msg = _msg("user", "Help me debug this error")
        assert c(msg)
        assert d(msg)
        assert n(msg)


class TestClassifierWiredIntoTranscript:
    def test_user_said_with_conductor_classifier_filters_synthetic(self):
        from captain_hook.classifiers.conductor import classifier
        from captain_hook.transcript import Transcript

        transcript = Transcript.from_parsed(
            [
                _msg("user", "<system_instruction>help me"),
                _msg("user", "debug this error"),
                _msg("assistant", "Sure, I can help"),
            ]
        )
        transcript.classifier = classifier
        assert transcript.user_said("debug")
        assert not transcript.user_said("help me")

    def test_user_said_without_classifier_matches_all_user_messages(self):
        from captain_hook.transcript import Transcript

        transcript = Transcript.from_parsed(
            [
                _msg("user", "<system_instruction>help me"),
                _msg("user", "debug this error"),
            ]
        )
        assert transcript.user_said("help me")
        assert transcript.user_said("debug")

    def test_first_user_message_with_classifier_skips_synthetic(self):
        from captain_hook.classifiers.conductor import classifier
        from captain_hook.transcript import Transcript

        transcript = Transcript.from_parsed(
            [
                _msg("user", "<system_instruction>You are helpful"),
                _msg("user", "Fix the bug"),
            ]
        )
        transcript.classifier = classifier
        assert transcript.first_user_message() == "Fix the bug"

    def test_first_user_message_without_classifier_returns_first(self):
        from captain_hook.transcript import Transcript

        transcript = Transcript.from_parsed(
            [
                _msg("user", "<system_instruction>You are helpful"),
                _msg("user", "Fix the bug"),
            ]
        )
        assert transcript.first_user_message() == "<system_instruction>You are helpful"


class TestClassifierAutoWiring:
    def test_from_messages_auto_wires_conductor_on_system_instruction(self):
        from captain_hook.transcript import Transcript

        raw = [
            raw_text("user", "<system_instruction>You are helpful"),
            raw_text("user", "Fix the bug"),
        ]
        transcript = Transcript.from_messages(raw)
        assert transcript.classifier is not None
        assert transcript.user_said("Fix")
        assert not transcript.user_said("system_instruction")

    def test_from_messages_auto_wires_and_filters_first_user_message(self):
        from captain_hook.transcript import Transcript

        raw = [
            raw_text("user", "<system_instruction>setup"),
            raw_text("user", "Real user message"),
        ]
        transcript = Transcript.from_messages(raw)
        assert transcript.first_user_message() == "Real user message"

    def test_from_path_auto_wires_conductor(self, tmp_path):
        import json

        from captain_hook.transcript import Transcript

        transcript_path = tmp_path / "conductor-workspaces" / "test.jsonl"
        transcript_path.parent.mkdir(parents=True)
        lines = [
            json.dumps(raw_text("user", "<system_instruction>setup")),
            json.dumps(raw_text("user", "Fix bug")),
        ]
        transcript_path.write_text("\n".join(lines))
        transcript = Transcript.from_path(transcript_path)
        assert transcript.classifier is not None
        assert transcript.first_user_message() == "Fix bug"

    def test_no_classifier_when_no_detection(self, monkeypatch):
        from captain_hook.transcript import Transcript

        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("FACTORY_PROJECT_DIR", raising=False)
        raw = [
            raw_text("user", "normal message"),
        ]
        transcript = Transcript.from_messages(raw)
        assert transcript.classifier is not None
