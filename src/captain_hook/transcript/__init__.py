from __future__ import annotations

import functools
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast, overload

from captain_hook.transcript.models import (
    ContentBlock,
    TaskNotification,
    TextBlock,
    ToolResult,
    ToolUse,
    ToolUseBlock,
    TranscriptMessage,
    parse_content,
    parse_content_block,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from captain_hook.command import Command
    from captain_hook.file import File
    from captain_hook.tools import EditOp, TaskOp, WriteOp

from captain_hook.types import TOOL_ALIASES, expand_tool_names, tool_name_matches


@dataclass(frozen=True)
class ToolUseQuery:
    """Chainable query builder for filtering and inspecting transcript tool uses.

    Use ``.where()`` to filter by name, file, error status, etc., and terminal
    methods like ``.count()``, ``.any()``, ``.first()``, ``.last()``, ``.files()`` to extract results.
    """

    items: list[ToolUse]

    def where(
        self,
        *,
        name: str | None = None,
        file: str | list[str] | None = None,
        file_under: str | list[str] | None = None,
        is_error: bool | None = None,
        input_has: dict[str, Any] | None = None,
    ) -> ToolUseQuery:
        def _match(tu: ToolUse) -> bool:
            if name and not any(tool_name_matches(tu.name, n) for n in name.split("|")):
                return False
            if file and not (
                tu.file
                and tu.file.matches(
                    *(file.split("|") if isinstance(file, str) else file),
                )
            ):
                return False
            if file_under and not (
                tu.file
                and tu.file.under(
                    *(file_under.split("|") if isinstance(file_under, str) else file_under),
                )
            ):
                return False
            if is_error is not None and tu.is_error != is_error:
                return False
            if input_has and not all(tu.raw_input.get(k) == v for k, v in input_has.items()):
                return False
            return True

        return ToolUseQuery([tu for tu in self.items if _match(tu)])

    def count(self) -> int:
        return len(self.items)

    def any(self) -> bool:
        return bool(self.items)

    def list(self) -> list[ToolUse]:
        return self.items

    def first(self) -> ToolUse | None:
        return self.items[0] if self.items else None

    def last(self) -> ToolUse | None:
        return self.items[-1] if self.items else None

    def files(self) -> list[File]:
        return [f for tu in self.items if (f := tu.file)]

    def __iter__(self) -> Iterator[ToolUse]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


class ToolUseSequence:
    """Sequence of tool uses that filters out errors by default.

    Access ``.with_errors`` for an unfiltered view. Use ``.where(...)`` to
    build a ``ToolUseQuery`` for chained filtering.
    """

    __slots__ = ("_all", "_include_errors", "_filtered")

    def __init__(self, items: list[ToolUse], *, _include_errors: bool = False) -> None:
        self._all = items
        self._include_errors = _include_errors
        self._filtered: list[ToolUse] | None = None

    @property
    def _items(self) -> list[ToolUse]:
        if self._include_errors:
            return self._all
        if self._filtered is None:
            self._filtered = [tu for tu in self._all if not tu.is_error]
        return self._filtered

    def where(self, **kwargs: Any) -> ToolUseQuery:
        return ToolUseQuery(self._items).where(**kwargs)

    def __getitem__(self, idx: int | slice) -> ToolUse | list[ToolUse]:
        return self._items[idx]

    def __iter__(self) -> Iterator[ToolUse]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    @property
    def with_errors(self) -> ToolUseSequence:
        return ToolUseSequence(self._all, _include_errors=True)


RawDict = dict[str, Any]


def try_json(text: str) -> RawDict | None:
    try:
        result: object = json.loads(text)
        return cast(RawDict, result) if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def extract_content(data: RawDict) -> list[Any]:
    msg = data.get("message")
    if isinstance(msg, dict):
        content = cast(RawDict, msg).get("content")
        return cast(list[Any], content) if isinstance(content, list) else []
    return []


def subagents(method: Callable[..., bool]) -> Callable[..., bool]:
    @functools.wraps(method)
    def wrapper(self: Transcript, *args: Any, subagents: bool = True, **kwargs: Any) -> bool:
        if (own := method(self, *args, **kwargs)) or not subagents:
            return own
        return any(getattr(sub, method.__name__)(*args, subagents=True, **kwargs) for sub in self.subagents)

    return wrapper


@dataclass
class Transcript:
    """The full session transcript: a sequence of messages with tool-use querying, slicing, and history checks."""

    TAIL_LINES: ClassVar[int] = 200

    messages: list[TranscriptMessage]
    path: Path | None = None
    classifier: Callable[[TranscriptMessage], bool] | None = field(default=None, repr=False)

    def is_user_message(self, msg: TranscriptMessage) -> bool:
        return self.classifier(msg) if self.classifier else msg.type == "user"

    def __len__(self) -> int:
        return len(self.messages)

    def __bool__(self) -> bool:
        return bool(self.messages)

    @overload
    def __getitem__(self, key: int) -> TranscriptMessage: ...
    @overload
    def __getitem__(self, key: slice) -> TranscriptSlice: ...
    def __getitem__(self, key: int | slice) -> TranscriptMessage | TranscriptSlice:
        match self.messages[key]:
            case list() as msgs:
                return TranscriptSlice(msgs)
            case msg:
                return msg

    def __str__(self) -> str:
        edits = self.tool_uses.where(name="Edit|Write")
        lines = self.full_text.splitlines()
        tail = lines[-self.TAIL_LINES :] if len(lines) > self.TAIL_LINES else lines
        parts: list[str] = [
            "<transcript>",
            "<metadata>",
        ]
        if self.path:
            parts.append(f"<path>{self.path}</path>")
        parts.extend(
            [
                f"<messages>{len(self.messages)}</messages>",
                f"<tool_uses>{len(self.tool_uses)}</tool_uses>",
                f"<edits>{edits.count()}</edits>",
                f"<errors>{self.count_failures()}</errors>",
            ]
        )
        parts.extend(f"<edited>{f}</edited>" for f in edits.files()[:10])
        parts.extend(
            [
                "</metadata>",
                "<tail>",
                "\n".join(tail),
                "</tail>",
                "</transcript>",
            ]
        )
        return "\n".join(parts)

    @classmethod
    def from_path(cls, path: Path | str | None) -> Transcript:
        if not path or not (path := Path(path)).exists():
            return cls([])
        messages = [
            TranscriptMessage.from_raw(
                type=str(data.get("type", "")),
                content=extract_content(data),
                raw=data,
            )
            for line in path.read_text().splitlines()
            if (stripped := line.strip()) and (data := try_json(stripped)) is not None
        ]
        t = cls(messages, path=path)
        t._auto_wire_classifier()
        return t

    @classmethod
    def from_simple_messages(cls, messages: list[dict[str, str]]) -> Transcript:
        return cls.from_messages([
            {
                "type": m["role"],
                "message": {"content": [{"type": "text", "text": m["content"]}]},
            }
            for m in messages
        ])

    @classmethod
    def from_messages(cls, messages: list[dict[str, Any]]) -> Transcript:
        t = cls(
            [
                TranscriptMessage.from_raw(
                    type=str(d.get("type", "")),
                    content=extract_content(d),
                    raw=d,
                )
                for d in messages
            ]
        )
        t._auto_wire_classifier()
        return t

    def _auto_wire_classifier(self) -> None:
        if self.classifier:
            return
        from captain_hook.classifiers import detect

        try:
            self.classifier = detect(
                cwd=os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("FACTORY_PROJECT_DIR"),
                transcript_path=str(self.path) if self.path else None,
                messages=self.messages or None,
            )
        except StopIteration:
            pass

    @classmethod
    def from_parsed(cls, messages: list[TranscriptMessage]) -> Transcript:
        return cls(messages)

    @cached_property
    def tool_uses(self) -> ToolUseSequence:
        result_map: dict[str, ToolResult] = {
            tr.tool_use_id: tr for msg in self.messages for tr in msg.tool_results if tr.tool_use_id
        }
        return ToolUseSequence(
            [
                ToolUse(
                    name=tu.name,
                    raw_input=tu.raw_input,
                    id=tu.id,
                    result=result_map.get(tu.id) if tu.id else None,
                    message_index=i,
                )
                for i, msg in enumerate(self.messages)
                for tu in msg.tool_uses
            ]
        )

    def count_tools(self, *names: str) -> int:
        return self.tool_uses.where(name="|".join(names)).count()

    @subagents
    def has_tool(self, name: str) -> bool:
        return self.tool_uses.where(name=name).any()

    @cached_property
    def commands(self) -> list[Command]:
        bash_names = expand_tool_names("Bash")
        return [
            cmd for tu in self.tool_uses if tu.name in bash_names and (cl := tu.command_line) for cmd in cl.commands
        ]

    @subagents
    def has_command(self, pattern: str) -> bool:
        return any(re.search(pattern, str(cmd)) for cmd in self.commands)

    @subagents
    def has_edit_to(self, *globs: str) -> bool:
        return any(f.matches(*globs) for f in self.tool_uses.where(name="Edit|Write").files())

    def user_said(self, *keywords: str) -> bool:
        return any(
            any(kw.lower() in msg.text.lower() for kw in keywords) for msg in self.messages if self.is_user_message(msg)
        )

    def all_edits_under(self, *prefixes: str) -> bool:
        return bool(files := self.tool_uses.where(name="Edit|Write").files()) and all(f.under(*prefixes) for f in files)

    def first_user_message(self) -> str | None:
        return next(
            (msg.text for msg in self.messages if self.is_user_message(msg)),
            None,
        )

    def after(self, tool: str, file: str | None = None) -> TranscriptSlice:
        last_idx = max(
            (
                tu.message_index
                for tu in self.tool_uses.with_errors
                if any(tool_name_matches(tu.name, t) for t in tool.split("|"))
                and (file is None or (tu.file and file in str(tu.file)))
            ),
            default=-1,
        )
        return self[last_idx + 1 :] if last_idx >= 0 else self[:0]

    def before(self, tool: str) -> TranscriptSlice:
        last_idx = max(
            (
                tu.message_index
                for tu in self.tool_uses.with_errors
                if any(tool_name_matches(tu.name, t) for t in tool.split("|"))
            ),
            default=len(self),
        )
        return self[:last_idx]

    def prior(self) -> TranscriptSlice:
        end = next(
            (i for i in range(len(self.messages), 0, -1) if self.messages[i - 1].type in ("assistant", "user")),
            0,
        )
        return self[: max(end - 1, 0)]

    def recent(self, n: int) -> TranscriptSlice:
        return self[-n:]

    @property
    def full_text(self) -> str:
        return "\n".join(msg.text for msg in self.messages)

    def assistant_text(self, n: int = 10, max_per_msg: int = 500) -> str:
        return "\n---\n".join(
            text[:max_per_msg]
            for msg in [m for m in self.messages if m.type == "assistant"][-n:]
            if (text := msg.text.strip())
        )

    def extract_files(self, tools: list[str] | None = None) -> list[File]:
        return (self.tool_uses.where(name="|".join(tools)) if tools else ToolUseQuery(list(self.tool_uses))).files()

    @subagents
    def has_read(self, pattern: str) -> bool:
        return any(pattern in str(f) for f in self.extract_files(["Read"]))

    @subagents
    def has_skill(self, *names: str) -> bool:
        return any(tu.raw_input.get("skill") in names for tu in self.tool_uses.where(name="Skill"))

    @cached_property
    def subagents(self) -> list[Transcript]:
        if not self.path or not (d := self.path.parent / self.path.stem / "subagents").is_dir():
            return []
        return [Transcript.from_path(p) for p in sorted(d.glob("*.jsonl"))]

    def count_failures(self) -> int:
        return sum(1 for msg in self.messages for tr in msg.tool_results if tr.is_error)

    def has_override(self, token: str, invalidated_by: list[str] | None = None) -> bool:
        inv = set(invalidated_by or ["Edit", "Write"])
        last = max(
            (i for i, msg in enumerate(self.messages) if token in msg.text or token in json.dumps(msg.raw or {})),
            default=-1,
        )
        return last >= 0 and not any(tu.message_index > last and tu.name in inv for tu in self.tool_uses.with_errors)

    def edit_ops(self) -> list[EditOp]:
        from captain_hook.tools import EditOp as EO

        return [op for tu in self.tool_uses.where(name="Edit") if (op := EO.parse(tu))]

    def write_ops(self) -> list[WriteOp]:
        from captain_hook.tools import WriteOp as WO

        return [op for tu in self.tool_uses.where(name="Write") if (op := WO.parse(tu))]

    def task_ops(self) -> list[TaskOp]:
        from captain_hook.tools import TaskOp as TO

        task_names = "TaskCreate|TaskUpdate|TaskGet|TaskList"
        return [op for tu in self.tool_uses.where(name=task_names) if (op := TO.parse(tu))]

    @cached_property
    def turn_start(self) -> int:
        return max(
            (i + 1 for i, msg in enumerate(self.messages) if self.is_user_message(msg)),
            default=0,
        )

    @cached_property
    def current_turn(self) -> Turn:
        return Turn(
            self.messages[(i := self.turn_start) :],
            path=self.path,
            start_idx=i,
            user_message=self.messages[i - 1] if i > 0 else None,
        )

    def since_last_user(self) -> Turn:
        return self.current_turn


class TranscriptSlice(Transcript):
    """A contiguous slice of a Transcript, returned by slicing operations like ``recent``, ``after``, ``before``."""

    pass


@dataclass(frozen=True)
class SubagentAccessor:
    id: str
    type: str
    transcript: Transcript
    parent_tool_use: ToolUse

    @property
    def tool_uses(self) -> ToolUseSequence:
        return self.transcript.tool_uses

    @property
    def failed(self) -> bool:
        return bool((r := self.parent_tool_use.result) and r.is_error) or any(
            tu.is_error for tu in self.transcript.tool_uses.with_errors
        )


@dataclass(frozen=True)
class SubagentRegistry:
    items: list[SubagentAccessor]

    def with_type(self, pattern: str) -> list[SubagentAccessor]:
        names = pattern.split("|")
        return [s for s in self.items if s.type in names]

    def __iter__(self) -> Iterator[SubagentAccessor]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


@dataclass
class Turn(TranscriptSlice):
    """The current conversation turn starting from the last user message, with edited-file tracking."""

    start_idx: int = 0
    user_message: TranscriptMessage | None = field(default=None, repr=False)

    @cached_property
    def user_text(self) -> str:
        return self.user_message.text if self.user_message else ""

    @cached_property
    def edited_files(self) -> set[File]:
        return set(self.extract_files(["Edit", "Write"]))

    def has_edit_under(self, *patterns: str) -> bool:
        return any(f.matches(*patterns) for f in self.edited_files)

    @cached_property
    def subagents(self) -> SubagentRegistry:
        if not self.path:
            return SubagentRegistry([])
        return SubagentRegistry([
            SubagentAccessor(
                id=tu.id,
                type=tu.agent_type or "",
                transcript=Transcript.from_path(
                    self.path.parent / self.path.stem / "subagents" / f"agent-{tu.id}.jsonl"
                ),
                parent_tool_use=tu,
            )
            for tu in self.tool_uses
            if tu.name in {"Agent", "Task"} and tu.agent_type and tu.id
        ])


__all__ = [
    "ContentBlock",
    "TaskNotification",
    "TextBlock",
    "SubagentAccessor",
    "SubagentRegistry",
    "ToolResult",
    "ToolUse",
    "ToolUseBlock",
    "ToolUseQuery",
    "ToolUseSequence",
    "Transcript",
    "TranscriptMessage",
    "TranscriptSlice",
    "Turn",
    "parse_content",
    "parse_content_block",
    "TOOL_ALIASES",
]
