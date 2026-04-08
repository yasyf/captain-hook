from __future__ import annotations

from pathlib import Path

from captain_hook.file import File
from captain_hook.transcript import Transcript, TranscriptMessage


def _make_transcript(*tool_uses: tuple[str, dict]) -> Transcript:
    messages = [
        TranscriptMessage.from_raw(
            type="assistant",
            content=[
                {
                    "type": "tool_use",
                    "name": name,
                    "input": raw_input,
                    "id": f"tu_{i}",
                }
                for i, (name, raw_input) in enumerate(tool_uses)
            ],
            raw={},
        ),
    ]
    return Transcript.from_parsed(messages)


class TestTranscriptMixinReturnTypes:
    def test_project_edit_count_returns_int(self) -> None:
        from hooks.mixins import (
            BioqaTranscriptMixin,
            apply_transcript_mixin,
            cleanup_mixins,
        )

        try:
            apply_transcript_mixin(BioqaTranscriptMixin)
            t = _make_transcript(
                ("Edit", {"file_path": "bioqa/core.py", "old_string": "a", "new_string": "b"}),
                ("Write", {"file_path": "www/index.tsx", "content": "x"}),
                ("Edit", {"file_path": "tests/test_foo.py", "old_string": "c", "new_string": "d"}),
            )
            result = t.project_edit_count
            assert isinstance(result, int), f"Expected int, got {type(result).__name__}: {result!r}"
            assert result == 2
        finally:
            cleanup_mixins()

    def test_task_stats_returns_tuple(self) -> None:
        from hooks.mixins import (
            BioqaTranscriptMixin,
            apply_transcript_mixin,
            cleanup_mixins,
        )

        try:
            apply_transcript_mixin(BioqaTranscriptMixin)
            t = _make_transcript(
                ("TaskCreate", {"title": "task1"}),
                ("TaskCreate", {"title": "task2"}),
                ("TaskUpdate", {"taskId": "t1", "status": "completed"}),
            )
            result = t.task_stats
            assert isinstance(result, tuple), f"Expected tuple, got {type(result).__name__}: {result!r}"
            assert result == (2, 1)
        finally:
            cleanup_mixins()

    def test_has_exploration_agent_is_callable(self) -> None:
        from hooks.mixins import (
            BioqaTranscriptMixin,
            apply_transcript_mixin,
            cleanup_mixins,
        )

        try:
            apply_transcript_mixin(BioqaTranscriptMixin)
            t = _make_transcript(
                ("Agent", {"subagent_type": "web-analyzer"}),
            )
            assert callable(t.has_exploration_agent)
            result = t.has_exploration_agent(["web-analyzer", "explore"])
            assert isinstance(result, bool)
            assert result is True

            t2 = _make_transcript(
                ("Agent", {"subagent_type": "feature-implementer"}),
            )
            assert t2.has_exploration_agent(["web-analyzer"]) is False
        finally:
            cleanup_mixins()


class TestIdempotency:
    def test_double_apply_file_mixin_is_noop(self) -> None:
        from hooks.mixins import (
            BioqaFileMixin,
            apply_file_mixin,
            cleanup_mixins,
        )

        try:
            apply_file_mixin(BioqaFileMixin)
            f = File(path=Path("bioqa/tests/util/modal/plugin.py"))
            assert f.is_infra is True

            apply_file_mixin(BioqaFileMixin)
            assert f.is_infra is True
        finally:
            cleanup_mixins()

    def test_double_apply_transcript_mixin_is_noop(self) -> None:
        from hooks.mixins import (
            BioqaTranscriptMixin,
            apply_transcript_mixin,
            cleanup_mixins,
        )

        try:
            apply_transcript_mixin(BioqaTranscriptMixin)
            apply_transcript_mixin(BioqaTranscriptMixin)
            t = _make_transcript(
                ("Edit", {"file_path": "bioqa/core.py", "old_string": "a", "new_string": "b"}),
            )
            result = t.project_edit_count
            assert isinstance(result, int)
            assert result == 1
        finally:
            cleanup_mixins()


class TestCleanup:
    def test_cleanup_removes_file_mixin_attrs(self) -> None:
        from hooks.mixins import (
            BioqaFileMixin,
            apply_file_mixin,
            cleanup_mixins,
        )

        apply_file_mixin(BioqaFileMixin)
        f = File(path=Path("bioqa/core.py"))
        assert hasattr(f, "is_infra")

        cleanup_mixins()
        assert not hasattr(File, "is_source")
        assert not hasattr(File, "is_infra")

    def test_cleanup_removes_transcript_mixin_attrs(self) -> None:
        from hooks.mixins import (
            BioqaTranscriptMixin,
            apply_transcript_mixin,
            cleanup_mixins,
        )

        apply_transcript_mixin(BioqaTranscriptMixin)
        assert hasattr(Transcript, "project_edit_count")

        cleanup_mixins()
        assert not hasattr(Transcript, "project_edit_count")
        assert not hasattr(Transcript, "task_stats")
        assert not hasattr(Transcript, "has_exploration_agent")

    def test_cleanup_allows_reapply(self) -> None:
        from hooks.mixins import (
            BioqaTranscriptMixin,
            apply_transcript_mixin,
            cleanup_mixins,
        )

        apply_transcript_mixin(BioqaTranscriptMixin)
        cleanup_mixins()
        apply_transcript_mixin(BioqaTranscriptMixin)
        t = _make_transcript(
            ("Edit", {"file_path": "bioqa/core.py", "old_string": "a", "new_string": "b"}),
        )
        result = t.project_edit_count
        assert isinstance(result, int)
        assert result == 1
        cleanup_mixins()
