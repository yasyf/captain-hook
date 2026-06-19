from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cc_transcript.activity import SessionActivity, meta_of
from cc_transcript.ids import SessionId
from cc_transcript.models import AssistantEvent, ToolUseBlock
from cc_transcript.parser import parse_events_from_bytes
from cc_transcript.query import Session
from cc_transcript.render import Budget, render_turn
from cc_transcript.tools import parse_tool_call
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from spawnllm import call, run_cli

from captain_hook.classifiers import detect
from captain_hook.llm import LlmBackends, TModel, TSpecialty
from captain_hook.prompt import Prompt
from captain_hook.session import SessionStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cc_transcript.models import TranscriptEvent
    from cc_transcript.tools import ToolCall


class LenientToolUseBlock(ToolUseBlock):
    """A ``ToolUseBlock`` whose typed parse degrades to ``OtherCall`` instead of raising.

    The hook runtime lifts every transcript on every event, so a Claude Code
    tool-shape change must degrade — with a still-correct digest — rather
    than crash every hook fire.
    """

    @property
    def call(self) -> ToolCall:
        return parse_tool_call(self.name, self.input, on_error="other")


def lenient_event(event: TranscriptEvent) -> TranscriptEvent:
    match event:
        case AssistantEvent(blocks=blocks) if any(isinstance(block, ToolUseBlock) for block in blocks):
            return replace(
                event,
                blocks=tuple(
                    LenientToolUseBlock(id=block.id, name=block.name, input=block.input)
                    if isinstance(block, ToolUseBlock)
                    else block
                    for block in blocks
                ),
            )
        case _:
            return event


def lift_session(events: Sequence[TranscriptEvent], *, path: Path | None = None) -> Session:
    """Lift parsed transcript events into a query ``Session``, injecting the detected user classifier."""
    from captain_hook.app import _state

    classifier = _state.classifier or detect(
        cwd=os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("FACTORY_PROJECT_DIR"),
        transcript_path=str(path) if path else None,
        events=events,
    )
    session_id = next(
        (meta.session_id for event in events if (meta := meta_of(event)) is not None),
        SessionId(path.stem if path else "unknown"),
    )
    return Session.from_activity(
        SessionActivity.from_events(session_id, [lenient_event(e) for e in events], user_classifier=classifier),
        path=path,
    )


def load_transcript(path: str | Path | None) -> Session:
    """Parse and lift the transcript at ``path``; a missing path yields an empty ``Session``."""
    if not path or not (path := Path(path)).exists():
        return Session(())
    return lift_session(parse_events_from_bytes(path.read_bytes()), path=path)


@dataclass
class HookContext:
    """Runtime context injected into every hook event: session state, transcript ``Session``, settings, and LLM/CLI helpers."""

    session: SessionStore
    transcript: Session
    settings: BaseSettings | None
    project_root: Path | None = None

    @property
    def t(self) -> Session:
        """Alias for ``transcript``."""
        return self.transcript

    @property
    def s(self) -> SessionStore:
        """Alias for ``session``."""
        return self.session

    @property
    def state(self) -> SessionStore:
        """Alias for ``session``."""
        return self.session

    @property
    def conf(self) -> BaseSettings | None:
        """Alias for ``settings``."""
        return self.settings

    @property
    def c(self) -> BaseSettings | None:
        """Alias for ``settings`` (shortest form)."""
        return self.conf

    @cached_property
    def turn(self) -> Session:
        """The one-turn view of the current turn (cached)."""
        return self.transcript.current_turn

    @cached_property
    def prior(self) -> Session:
        """The session window before the current turn's last exchange (cached)."""
        return self.transcript.prior()

    def transcript_text(self) -> str:
        """The transcript rendered turn by turn under the default budget."""
        return "\n\n".join(
            rendered for turn in self.transcript.turns if (rendered := render_turn(turn, budget=Budget()))
        )

    def call_cli(
        self,
        args: list[str],
        *,
        input: str | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> str:
        return run_cli(
            args,
            input=input,
            timeout=timeout,
            env=os.environ | (env or {}),
            cwd=os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("FACTORY_PROJECT_DIR"),
        )

    def git(self, *args: str) -> str | None:
        try:
            return self.call_cli(["git", *args], timeout=5)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    @cached_property
    def changed_paths(self) -> frozenset[Path] | None:
        if (out := self.git("diff", "--name-only", "HEAD", "--no-renames")) is None or (root := self.repo_root) is None:
            return None
        return frozenset((root / line).resolve() for line in out.splitlines() if line)

    @cached_property
    def repo_root(self) -> Path | None:
        if self.project_root is not None:
            return self.project_root.resolve()
        return Path(out.strip()) if (out := self.git("rev-parse", "--show-toplevel")) else None

    @cached_property
    def current_branch(self) -> str | None:
        return out.strip() if (out := self.git("symbolic-ref", "--short", "HEAD")) else None

    def call_llm(
        self,
        template: str | Prompt,
        *args: Any,
        specialty: TSpecialty = "general",
        model: TModel = "small",
        timeout: int = 180,
        transcript: bool = False,
        agent: bool = False,
        response_model: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> str | BaseModel:
        if isinstance(template, Prompt):
            prompt = str(template)
            if transcript:
                prompt = f"{self.transcript_text()}\n\n<task>\n{prompt}\n</task>"
        else:
            if transcript:
                template = f"{{transcript}}\n\n<task>\n{template}\n</task>"
            prompt = template.format(*args, **kwargs, transcript=self.transcript_text())
        return call(
            prompt,
            backend=LlmBackends.for_specialty(specialty),
            model=model,
            agent=agent,
            response_model=response_model,
            cwd=os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("FACTORY_PROJECT_DIR"),
            timeout=timeout,
        )
