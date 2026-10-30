from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.events import StopEvent
from captain_hook.tasks import Task, Tasks
from captain_hook.util import reqenv


def write_task(list_dir: Path, task_id: str, status: str, **extra: Any) -> None:
    list_dir.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {"id": task_id, "subject": f"task {task_id}", "status": status, "blocks": [], "blockedBy": []}
    raw |= extra
    (list_dir / f"{task_id}.json").write_text(json.dumps(raw))


def metadata_io(monkeypatch: pytest.MonkeyPatch, directory: Path) -> dict[str, int]:
    counts = {"stat": 0, "read": 0}
    guard = threading.Lock()
    original_stat, original_read = Path.stat, Path.read_text

    def stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path.parent == directory and path.name.endswith(".meta.json"):
            with guard:
                counts["stat"] += 1
        return original_stat(path, follow_symlinks=follow_symlinks)

    def read(path: Path, encoding: str | None = None, errors: str | None = None, newline: str | None = None) -> str:
        if path.parent == directory and path.name.endswith(".meta.json"):
            with guard:
                counts["read"] += 1
        return original_read(path, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "read_text", read)
    return counts


@pytest.fixture
def tasks_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tasks"
    monkeypatch.setenv("CAPTAIN_HOOK_TASKS_DIR", str(root))
    return root


class TestResolveRoot:
    def test_env_override(self, tasks_root: Path) -> None:
        assert Tasks.resolve_root() == tasks_root

    def test_claude_config_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("CAPTAIN_HOOK_TASKS_DIR", raising=False)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        assert Tasks.resolve_root() == tmp_path / "cfg" / "tasks"

    def test_default_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CAPTAIN_HOOK_TASKS_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert Tasks.resolve_root() == Path.home() / ".claude" / "tasks"


class TestTaskParsing:
    def test_from_raw(self) -> None:
        task = Task.from_raw(
            {
                "id": "6",
                "subject": "Quality gate",
                "description": "Run it.",
                "status": "pending",
                "owner": "lead",
                "blocks": ["7"],
                "blockedBy": ["5"],
            },
        )
        assert task.id == "6"
        assert task.subject == "Quality gate"
        assert task.owner == "lead"
        assert task.blocked_by == ("5",)
        assert task.blocks == ("7",)
        assert task.is_open

    def test_from_raw_defaults(self) -> None:
        task = Task.from_raw({"id": 3})
        assert task.id == "3"
        assert task.status == "pending"
        assert task.subject == ""
        assert task.owner is None

    def test_is_open_by_status(self) -> None:
        assert Task.from_raw({"id": "1", "status": "in_progress"}).is_open
        assert not Task.from_raw({"id": "1", "status": "completed"}).is_open


class TestTasksForSession:
    def test_loads_only_exact_session(self, tasks_root: Path) -> None:
        write_task(tasks_root / "stale-session", "1", "pending")
        write_task(tasks_root / "current", "1", "completed")
        tasks = Tasks.for_session("current")
        assert len(tasks) == 1
        assert tasks.all_completed

    def test_missing_store_is_empty(self, tasks_root: Path) -> None:
        write_task(tasks_root / "other", "1", "pending")
        tasks = Tasks.for_session("no-such-session")
        assert not tasks
        assert tasks.all_completed

    def test_empty_session_id_is_empty(self, tasks_root: Path) -> None:
        write_task(tasks_root / "s", "1", "pending")
        assert not Tasks.for_session("")

    def test_sorted_numerically(self, tasks_root: Path) -> None:
        for task_id in ["10", "2", "1"]:
            write_task(tasks_root / "s", task_id, "pending")
        assert [t.id for t in Tasks.for_session("s")] == ["1", "2", "10"]

    def test_ignores_non_json_and_malformed(self, tasks_root: Path) -> None:
        write_task(tasks_root / "s", "1", "pending")
        (tasks_root / "s" / ".lock").write_text("")
        (tasks_root / "s" / "2.json").write_text("{not json")
        assert len(Tasks.for_session("s")) == 1

    def test_explicit_root(self, tmp_path: Path) -> None:
        write_task(tmp_path / "custom" / "s", "1", "completed")
        assert len(Tasks.for_session("s", root=tmp_path / "custom")) == 1

    def test_loads_truncated_session_dir(self, tasks_root: Path) -> None:
        # Current Claude Code names the store session-<first 8 chars of the session id>.
        session_id = "c7b2de52-4222-4089-a2a2-14f3e9844d8f"
        write_task(tasks_root / "session-c7b2de52", "1", "pending")
        tasks = Tasks.for_session(session_id)
        assert len(tasks) == 1
        assert not tasks.all_completed

    def test_prefers_exact_id_over_truncated(self, tasks_root: Path) -> None:
        # Both namings on disk: the exact-session-id dir (legacy) wins over session-<first8>.
        session_id = "c7b2de52-4222-4089-a2a2-14f3e9844d8f"
        write_task(tasks_root / session_id, "1", "completed")
        write_task(tasks_root / "session-c7b2de52", "1", "pending")
        tasks = Tasks.for_session(session_id)
        assert len(tasks) == 1
        assert tasks.all_completed  # the pending task in the truncated dir is never read


class TestListId:
    def write_meta(self, transcript: Path, name: str, **meta: Any) -> None:
        subagents = transcript.with_suffix("") / "subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        (subagents / f"agent-{name}.meta.json").write_text(json.dumps({"name": name} | meta))

    def test_session_without_a_team_reads_its_own_list(self, tmp_path: Path) -> None:
        transcript = tmp_path / "s.jsonl"
        self.write_meta(transcript, "explorer")
        assert Tasks.list_id("s", transcript) == "s"
        assert Tasks.list_id("s", None) == "s"

    def test_resumed_team_lead_reads_its_team_list(self, tasks_root: Path, tmp_path: Path) -> None:
        transcript = tmp_path / "900424b6.jsonl"
        self.write_meta(transcript, "lane", teamName="session-756e25cc")
        write_task(tasks_root / "session-756e25cc", "1", "in_progress")
        assert Tasks.list_id("900424b6", transcript) == "session-756e25cc"
        assert len(Tasks.for_session(Tasks.list_id("900424b6", transcript))) == 1

    def test_lead_resumed_into_a_new_team_reads_the_newest_teams_list(self, tasks_root: Path, tmp_path: Path) -> None:
        transcript = tmp_path / "900424b6.jsonl"
        subagents = transcript.with_suffix("") / "subagents"
        metas = [("a-old", "session-756e25cc"), ("b-new", "session-67c0e5da"), ("c-old", "session-756e25cc")]
        for index, (name, team) in enumerate(metas):
            self.write_meta(transcript, name, teamName=team)
            os.utime(subagents / f"agent-{name}.meta.json", (1_000 + index, 1_000 + index))
        os.utime(subagents / "agent-b-new.meta.json", (2_000, 2_000))
        write_task(tasks_root / "session-756e25cc", "1", "completed")
        write_task(tasks_root / "session-67c0e5da", "7", "in_progress")
        assert Tasks.list_id("67c0e5da", transcript) == "session-67c0e5da"
        assert [task.id for task in Tasks.for_session(Tasks.list_id("67c0e5da", transcript))] == ["7"]

    def test_team_name_is_sanitized(self, tmp_path: Path) -> None:
        transcript = tmp_path / "s.jsonl"
        self.write_meta(transcript, "lane", teamName="release v3")
        assert Tasks.list_id("s", transcript) == "release-v3"

    def test_explicit_list_id_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        transcript = tmp_path / "s.jsonl"
        self.write_meta(transcript, "lane", teamName="team")
        monkeypatch.setenv("CLAUDE_CODE_TASK_LIST_ID", "shared")
        assert Tasks.list_id("s", transcript) == "shared"

    def test_request_resolves_metadata_once_for_all_hook_copies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transcript = tmp_path / "s.jsonl"
        for index in range(16):
            self.write_meta(transcript, str(index))
        counts = metadata_io(monkeypatch, transcript.with_suffix("") / "subagents")
        overrides = reqenv.RequestOverrides(env={}, cwd=str(tmp_path), client_ppid=1, session_id="s")

        with reqenv.use_request(overrides):
            assert [Tasks.list_id("s", transcript) for _ in range(4)] == ["s"] * 4

        assert counts == {"stat": 16, "read": 16}

    def test_concurrent_hook_copies_share_one_metadata_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transcript = tmp_path / "s.jsonl"
        for index in range(16):
            self.write_meta(transcript, str(index))
        counts = metadata_io(monkeypatch, transcript.with_suffix("") / "subagents")
        overrides = reqenv.RequestOverrides(env={}, cwd=str(tmp_path), client_ppid=1, session_id="s")
        ready = threading.Barrier(4)

        def resolve() -> str:
            with reqenv.use_request(overrides):
                ready.wait()
                return Tasks.list_id("s", transcript)

        with ThreadPoolExecutor(max_workers=4) as pool:
            assert [future.result() for future in [pool.submit(resolve) for _ in range(4)]] == ["s"] * 4

        assert counts == {"stat": 16, "read": 16}

    def test_new_requests_resolve_the_latest_team(self, tmp_path: Path) -> None:
        transcript = tmp_path / "s.jsonl"
        self.write_meta(transcript, "lane", teamName="first")
        first = reqenv.RequestOverrides(env={}, cwd=str(tmp_path), client_ppid=1, session_id="s")
        second = reqenv.RequestOverrides(env={}, cwd=str(tmp_path), client_ppid=1, session_id="s")

        with reqenv.use_request(first):
            assert Tasks.list_id("s", transcript) == "first"
            self.write_meta(transcript, "lane", teamName="second")
            assert Tasks.list_id("s", transcript) == "first"
        with reqenv.use_request(second):
            assert Tasks.list_id("s", transcript) == "second"

    def test_unbound_calls_keep_resolving_fresh_metadata(self, tmp_path: Path) -> None:
        transcript = tmp_path / "s.jsonl"
        self.write_meta(transcript, "lane", teamName="first")
        assert Tasks.list_id("s", transcript) == "first"
        self.write_meta(transcript, "lane", teamName="second")
        assert Tasks.list_id("s", transcript) == "second"

    def test_request_keeps_session_and_transcript_identities_separate(self, tmp_path: Path) -> None:
        one, two = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
        self.write_meta(one, "lane", teamName="first")
        self.write_meta(two, "lane", teamName="second")
        overrides = reqenv.RequestOverrides(env={}, cwd=str(tmp_path), client_ppid=1, session_id="s")

        with reqenv.use_request(overrides):
            assert Tasks.list_id("s", one) == "first"
            assert Tasks.list_id("s", two) == "second"
            assert Tasks.list_id("one", None) == "one"
            assert Tasks.list_id("two", None) == "two"

    def test_explicit_identity_does_not_enumerate_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transcript = tmp_path / "s.jsonl"
        self.write_meta(transcript, "lane", teamName="team")
        counts = metadata_io(monkeypatch, transcript.with_suffix("") / "subagents")
        overrides = reqenv.RequestOverrides(
            env={"CLAUDE_CODE_TASK_LIST_ID": "explicit"}, cwd=str(tmp_path), client_ppid=1, session_id="s"
        )

        with reqenv.use_request(overrides):
            assert Tasks.list_id("s", transcript) == "explicit"
        assert counts == {"stat": 0, "read": 0}

    def test_failed_resolution_is_not_retained(self, tmp_path: Path) -> None:
        transcript = tmp_path / "s.jsonl"
        self.write_meta(transcript, "lane", teamName="team")
        meta = transcript.with_suffix("") / "subagents" / "agent-lane.meta.json"
        meta.write_text("{")
        overrides = reqenv.RequestOverrides(env={}, cwd=str(tmp_path), client_ppid=1, session_id="s")

        with reqenv.use_request(overrides):
            with pytest.raises(json.JSONDecodeError):
                Tasks.list_id("s", transcript)
            self.write_meta(transcript, "lane", teamName="repaired")
            assert Tasks.list_id("s", transcript) == "repaired"


class TestTasksQuerying:
    def make(self, *statuses: str) -> Tasks:
        return Tasks(tuple(Task.from_raw({"id": str(i + 1), "status": s}) for i, s in enumerate(statuses)))

    def test_status_filters(self) -> None:
        tasks = self.make("pending", "in_progress", "completed", "completed")
        assert len(tasks.pending) == 1
        assert len(tasks.in_progress) == 1
        assert len(tasks.completed) == 2
        assert len(tasks.open) == 2
        assert not tasks.all_completed

    def test_all_completed(self) -> None:
        assert self.make("completed", "completed").all_completed
        assert self.make().all_completed

    def test_get(self) -> None:
        tasks = self.make("pending", "completed")
        assert (task := tasks.get("2")) and task.status == "completed"
        assert tasks.get("99") is None

    def test_sequence_protocol(self) -> None:
        tasks = self.make("pending", "completed")
        assert tasks[0].id == "1"
        assert tasks[:1] == (tasks[0],)
        assert [t.id for t in tasks] == ["1", "2"]
        assert tasks[1] in tasks


class TestEventTasks:
    def test_event_reads_payload_session(self, tasks_root: Path) -> None:
        write_task(tasks_root / "sess-1", "1", "pending")
        evt = StopEvent(_raw={"session_id": "sess-1"}, ctx=MagicMock())
        assert evt.session_id == "sess-1"
        assert len(evt.tasks) == 1
        assert not evt.tasks.all_completed

    def test_event_without_store_sees_no_tasks(self, tasks_root: Path) -> None:
        write_task(tasks_root / "someone-else", "1", "pending")
        evt = StopEvent(_raw={"session_id": "sess-2"}, ctx=MagicMock())
        assert not evt.tasks
        assert evt.tasks.all_completed

    def test_copies_and_background_keep_task_contents_fresh(self, tasks_root: Path, tmp_path: Path) -> None:
        transcript = tmp_path / "s.jsonl"
        TestListId().write_meta(transcript, "lane", teamName="team")
        write_task(tasks_root / "team", "1", "pending")
        event = StopEvent(_raw={"session_id": "s", "transcript_path": str(transcript)}, ctx=MagicMock())
        first, second = copy(event), copy(event)
        overrides = reqenv.RequestOverrides(
            env={"CAPTAIN_HOOK_TASKS_DIR": str(tasks_root)}, cwd=str(tmp_path), client_ppid=1, session_id="s"
        )

        with reqenv.use_request(overrides):
            assert first.tasks[0].status == "pending"
            write_task(tasks_root / "team", "1", "completed")
            assert second.tasks[0].status == "completed"
            background = StopEvent(_raw=event._raw, ctx=MagicMock())
            assert background.tasks[0].status == "completed"
            assert first.tasks[0].status == "pending"
