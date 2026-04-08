from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from captain_hook.transcript.inputs import EditInput, TaskCreateInput, TaskUpdateInput, WriteInput
from captain_hook.transcript.models import ToolUse

if TYPE_CHECKING:
    from captain_hook.file import File


@dataclass(frozen=True)
class EditOp:
    """A parsed Edit tool operation extracted from a transcript tool use."""

    file_path: File
    old_string: str
    new_string: str

    @classmethod
    def parse(cls, tu: ToolUse) -> EditOp | None:
        return cls(ti.file, ti.old, ti.new) if isinstance(ti := tu.input, EditInput) and ti.file_path else None


@dataclass(frozen=True)
class WriteOp:
    """A parsed Write/Create tool operation extracted from a transcript tool use."""

    file_path: File
    content: str

    @classmethod
    def parse(cls, tu: ToolUse) -> WriteOp | None:
        return cls(ti.file, ti.content) if isinstance(ti := tu.input, WriteInput) and ti.file_path else None


@dataclass(frozen=True)
class TaskOp:
    """A parsed task-tracker operation (create/update/get/list) extracted from a transcript tool use."""

    action: Literal["create", "update", "get", "list"]
    task_id: str | None = None
    status: str | None = None
    title: str | None = None

    @classmethod
    def parse(cls, tu: ToolUse) -> TaskOp | None:
        match tu.input:
            case TaskCreateInput() as ti:
                return cls("create", title=ti.subject or None, status=None)
            case TaskUpdateInput() as ti:
                return cls("update", ti.task_id or None, ti.status or None)
            case _ if tu.name == "TaskGet":
                return cls("get", tu.raw_input.get("id"))
            case _ if tu.name == "TaskList":
                return cls("list")
            case _:
                return None
