from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, TypeVar, cast, overload

from funcy import project  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from captain_hook.file import File

T = TypeVar("T")

RawDict = dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class InputBase:
    """Base class for typed tool inputs. Provides ``from_raw()`` parsing and ``as_()`` type narrowing."""

    raw: RawDict = field(default_factory=lambda: {}, repr=False)

    @classmethod
    def from_raw(cls, raw: RawDict) -> Self:
        fields = {f.name for f in dataclasses.fields(cls)} - {"raw"}
        return cls(raw=raw, **cast(RawDict, project(raw, fields)))

    @overload
    def as_(self, cls: type[T]) -> T | None: ...
    @overload
    def as_(self, cls: type[T], default: T) -> T: ...
    def as_(self, cls: type[T], default: T | None = None) -> T | None:
        return self if isinstance(self, cls) else default


@dataclass(frozen=True, kw_only=True)
class FileInputBase(InputBase):
    """Base for tool inputs that reference a file, providing a cached ``file`` property returning a ``File``."""

    file_path: str

    @cached_property
    def file(self) -> File:
        from captain_hook.file import File as F

        return F(path=Path(self.file_path))


@dataclass(frozen=True, kw_only=True)
class BashInput(InputBase):
    """Parsed Bash/Execute tool input."""

    command: str
    timeout: int | None = None
    description: str | None = None


@dataclass(frozen=True, kw_only=True)
class EditInput(FileInputBase):
    """Parsed Edit tool input with old/new content for replacements."""

    old: str
    new: str
    replace_all: bool = False

    @classmethod
    def from_raw(cls, raw: RawDict) -> EditInput:
        return cls(
            raw=raw,
            file_path=raw["file_path"],
            old=raw["old_string"],
            new=raw["new_string"],
            **cast(RawDict, project(raw, ["replace_all"])),
        )


@dataclass(frozen=True, kw_only=True)
class WriteInput(FileInputBase):
    """Parsed Write/Create tool input."""

    content: str


@dataclass(frozen=True, kw_only=True)
class ReadInput(FileInputBase):
    """Parsed Read tool input."""

    limit: int | None = None
    offset: int | None = None


@dataclass(frozen=True, kw_only=True)
class AgentInput(InputBase):
    """Parsed Agent/Task tool input."""

    prompt: str
    agent_type: str | None = None
    model: str | None = None
    name: str | None = None
    run_in_background: bool | None = None

    @classmethod
    def from_raw(cls, raw: RawDict) -> AgentInput:
        return cls(
            raw=raw,
            prompt=raw["prompt"],
            agent_type=raw.get("subagent_type") or raw.get("agent_type"),
            **cast(RawDict, project(raw, ["model", "name", "run_in_background"])),
        )


@dataclass(frozen=True, kw_only=True)
class GrepInput(InputBase):
    """Parsed Grep tool input."""

    pattern: str
    path: str | None = None
    file_type: str | None = None
    glob: str | None = None
    output_mode: str | None = None

    @classmethod
    def from_raw(cls, raw: RawDict) -> GrepInput:
        return cls(
            raw=raw,
            pattern=raw["pattern"],
            file_type=raw.get("type"),
            **cast(RawDict, project(raw, ["path", "glob", "output_mode"])),
        )


@dataclass(frozen=True, kw_only=True)
class GlobInput(InputBase):
    """Parsed Glob tool input."""

    pattern: str
    path: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskCreateInput(InputBase):
    """Parsed TaskCreate tool input."""

    subject: str
    description: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskUpdateInput(InputBase):
    """Parsed TaskUpdate tool input."""

    task_id: str
    status: str | None = None
    subject: str | None = None
    description: str | None = None

    @classmethod
    def from_raw(cls, raw: RawDict) -> TaskUpdateInput:
        return cls(
            raw=raw,
            task_id=raw.get("taskId") or raw["task_id"],
            **cast(RawDict, project(raw, ["status", "subject", "description"])),
        )


@dataclass(frozen=True, kw_only=True)
class SkillInput(InputBase):
    """Parsed Skill tool input."""

    skill: str
    args: str | None = None


@dataclass(frozen=True, kw_only=True)
class GenericInput(InputBase):
    """Fallback typed input for unrecognized tools, providing dict-like ``get()`` access to raw data."""

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)


ToolInput = (
    BashInput
    | EditInput
    | WriteInput
    | ReadInput
    | AgentInput
    | GrepInput
    | GlobInput
    | TaskCreateInput
    | TaskUpdateInput
    | SkillInput
    | GenericInput
)

FileInput = EditInput | WriteInput | ReadInput

TOOL_PARSERS: dict[str, type[InputBase]] = {
    "Bash": BashInput,
    "Edit": EditInput,
    "MultiEdit": EditInput,
    "Write": WriteInput,
    "Read": ReadInput,
    "Agent": AgentInput,
    "Grep": GrepInput,
    "Glob": GlobInput,
    "TaskCreate": TaskCreateInput,
    "TaskUpdate": TaskUpdateInput,
    "Skill": SkillInput,
    "Execute": BashInput,
    "Create": WriteInput,
    "Task": AgentInput,
}


def parse_tool_input(name: str, raw: RawDict | Any) -> ToolInput:
    """Parse raw tool input into a typed dataclass based on tool name."""
    normalized = cast(RawDict, raw) if isinstance(raw, dict) else cast(RawDict, {})
    if not (cls := TOOL_PARSERS.get(name)):
        return GenericInput(raw=normalized)
    try:
        return cast(ToolInput, cls.from_raw(normalized))
    except (KeyError, TypeError):
        return GenericInput(raw=normalized)
