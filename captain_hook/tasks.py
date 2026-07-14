from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, overload

from loguru import logger

from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_claude_config_dir


@dataclass(frozen=True, kw_only=True)
class Task:
    """A task read from Claude Code's native task store (``~/.claude/tasks/<list-id>/<id>.json``)."""

    OPEN_STATUSES: ClassVar[tuple[str, ...]] = ("pending", "in_progress")

    id: str
    subject: str
    status: str
    description: str = ""
    owner: str | None = None
    blocked_by: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Task:
        return cls(
            id=str(raw.get("id", "")),
            subject=raw.get("subject") or "",
            status=raw.get("status") or "pending",
            description=raw.get("description") or "",
            owner=raw.get("owner") or None,
            blocked_by=tuple(raw.get("blockedBy") or ()),
            blocks=tuple(raw.get("blocks") or ()),
        )


@dataclass(frozen=True)
class Tasks(Sequence[Task]):
    """The live task list for one session, read from the native store rather than the transcript.

    Always keyed by the exact list id (session id) — a session with no store has no
    tasks, never another session's. This is the source of truth for completion gates;
    transcript-derived ``task_ops()`` misses updates made by subagents, teammates, or
    resumed sessions.
    """

    tasks: tuple[Task, ...] = ()

    @classmethod
    def resolve_root(cls) -> Path:
        """Resolve the root of Claude Code's native task store (``<config-dir>/tasks``)."""
        if explicit := reqenv.getenv("CAPTAIN_HOOK_TASKS_DIR"):
            return Path(explicit)
        return resolve_claude_config_dir() / "tasks"

    @classmethod
    def for_session(cls, session_id: str, *, root: Path | None = None) -> Tasks:
        """Load the task list for ``session_id``, empty when absent.

        Claude Code names the store dir either by the exact session id (legacy) or
        ``session-<first-8-chars>`` (current); an exact-id dir wins over the truncated one.
        """
        if not session_id:
            return cls()
        base = root or cls.resolve_root()
        list_dir = next(
            (d for name in (session_id, f"session-{session_id[:8]}") if (d := base / name).is_dir()),
            None,
        )
        if list_dir is None:
            return cls()
        tasks: list[Task] = []
        for path in list_dir.glob("*.json"):
            try:
                tasks.append(Task.from_raw(json.loads(path.read_text())))
            except (OSError, ValueError):
                logger.bind(path=str(path)).opt(exception=True).warning("failed to read task file")
        return cls(tuple(sorted(tasks, key=lambda t: (not t.id.isdigit(), int(t.id) if t.id.isdigit() else 0, t.id))))

    @overload
    def __getitem__(self, index: int) -> Task: ...
    @overload
    def __getitem__(self, index: slice) -> tuple[Task, ...]: ...
    def __getitem__(self, index: int | slice) -> Task | tuple[Task, ...]:
        return self.tasks[index]

    def __len__(self) -> int:
        return len(self.tasks)

    def get(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def with_status(self, *statuses: str) -> tuple[Task, ...]:
        return tuple(t for t in self.tasks if t.status in statuses)

    @property
    def pending(self) -> tuple[Task, ...]:
        return self.with_status("pending")

    @property
    def in_progress(self) -> tuple[Task, ...]:
        return self.with_status("in_progress")

    @property
    def completed(self) -> tuple[Task, ...]:
        return self.with_status("completed")

    @property
    def open(self) -> tuple[Task, ...]:
        return self.with_status(*Task.OPEN_STATUSES)

    @property
    def all_completed(self) -> bool:
        return not self.open
