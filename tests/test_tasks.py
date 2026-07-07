from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.events import StopEvent
from captain_hook.tasks import Task, Tasks


def write_task(list_dir: Path, task_id: str, status: str, **extra: Any) -> None:
    list_dir.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {"id": task_id, "subject": f"task {task_id}", "status": status, "blocks": [], "blockedBy": []}
    raw |= extra
    (list_dir / f"{task_id}.json").write_text(json.dumps(raw))


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
