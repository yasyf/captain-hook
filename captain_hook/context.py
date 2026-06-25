from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

from cc_transcript.activity import SessionActivity, meta_of
from cc_transcript.ids import SessionId
from cc_transcript.models import AssistantEvent, ToolUseBlock
from cc_transcript.parser import parse_events_from_bytes
from cc_transcript.query import Session
from cc_transcript.render import Budget, render_turn
from cc_transcript.tools import parse_tool_call
from pydantic import BaseModel
from spawnllm import call_sync, extract_sync
from spawnllm.proc import run_cli

from captain_hook.classifiers import detect
from captain_hook.llm import LlmBackends, TModel, TSpecialty
from captain_hook.prompt import Prompt
from captain_hook.session import SessionStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cc_transcript.models import TranscriptEvent
    from cc_transcript.tools import ToolCall

    from captain_hook.settings import HooksSettings


RECENT_WINDOW = 15


def transcript_window(transcript: bool | int | Literal["recent", "full"]) -> int | None:
    match transcript:
        case "full":
            return None
        case True | "recent":
            return RECENT_WINDOW
        case int() as events:
            return events


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
    """Runtime context injected into every hook event.

    Holds session state, the transcript ``Session``, settings, and LLM/CLI helpers.
    """

    session: SessionStore
    transcript: Session
    settings: HooksSettings | None
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
    def conf(self) -> HooksSettings | None:
        """Alias for ``settings``."""
        return self.settings

    @property
    def c(self) -> HooksSettings | None:
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

    def transcript_text(self, *, window: int | None = None) -> str:
        """The transcript rendered turn by turn under the default budget.

        Args:
            window: Render only the most recent ``window`` events; ``None`` renders the whole session.
        """
        src = self.transcript if window is None else self.transcript.recent(window)
        return "\n\n".join(rendered for turn in src.turns if (rendered := render_turn(turn, budget=Budget())))

    def transcript_block(self, *, window: int | None = RECENT_WINDOW) -> str:
        """The rendered transcript wrapped in a ``<transcript>`` tag carrying its source path.

        Defaults to a recent-event window rather than the whole session. The render clips long
        turns and tool calls under :class:`Budget`, so an agent-mode LLM uses the path to read
        the untruncated content (e.g. a full ``ExitPlanMode`` plan) or earlier history.

        Args:
            window: Render only the most recent ``window`` events; ``None`` renders the whole session.
        """
        rendered = self.transcript_text(window=window)
        if (path := self.transcript.path) is not None:
            return f'<transcript path="{path}">\n{rendered}\n</transcript>'
        return f"<transcript>\n{rendered}\n</transcript>"

    def call_cli(
        self,
        args: list[str],
        *,
        input: str | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
        throw: bool = True,
    ) -> str | None:
        try:
            return run_cli(
                args,
                input=input,
                timeout=timeout,
                env=os.environ | (env or {}),
                cwd=os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("FACTORY_PROJECT_DIR"),
            )
        except (OSError, subprocess.SubprocessError):
            if throw:
                raise
            return None

    def git(self, *args: str) -> str | None:
        try:
            return self.call_cli(["git", *args], timeout=5)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    def diff(
        self,
        source: str = "uncommitted",
        *,
        commit: str | None = None,
        scope: str | None = None,
        budget: int = 4000,
    ) -> str | None:
        """A compact diff via ``ccx diff`` when available, else plain ``git``.

        Prefers cc-context's token-budgeted ``ccx diff`` and falls back to ``git`` when ``ccx`` is
        absent, fails, or returns a hunkless symbol-outline (no ``@@`` / ``diff --git`` lines, as in
        jj-colocated repos), so a hook gets a real diff in any repo. The git fallback is bounded to
        roughly ``budget`` tokens with a trailing marker when truncated, so a large diff can't blow
        the caller's context.

        Args:
            source: ``"uncommitted"`` (the default), ``"staged"``, or any git ref. Ignored when
                ``commit`` is set.
            commit: When set, the diff *introduced by* this commit (root-commit-safe), overriding
                ``source``; its git fallback is ``git show --stat -p``, so it carries the commit
                header + diffstat ahead of the patch.
            scope: Restrict the diff to this path.
            budget: Token budget for both the ``ccx`` output and the bounded git fallback.
        """
        target = f"{commit}~1..{commit}" if commit is not None else source
        if (out := self.ccx_diff(target, scope=scope, budget=budget)) is not None:
            return out
        git_scope = ("--", scope) if scope else ()
        if commit is not None:
            return self.bounded_git(budget, "show", "--stat", "-p", commit, *git_scope)
        match source:
            case "uncommitted":
                args = ["diff"]
            case "staged":
                args = ["diff", "--staged"]
            case ref:
                args = ["diff", ref]
        return self.bounded_git(budget, *args, *git_scope)

    def ccx_diff(self, target: str, *, scope: str | None, budget: int) -> str | None:
        cmd = ["ccx", "diff", target, "--budget", str(budget), *(("--scope", scope) if scope else ())]
        out = self.call_cli(cmd, throw=False)
        return out if out is not None and ("@@" in out or "diff --git" in out) else None

    def bounded_git(self, budget: int, *args: str) -> str | None:
        if (out := self.git(*args)) is None or len(out) <= (limit := budget * 4):
            return out
        return out[:limit].rstrip() + f"\n... [diff truncated to ~{budget} tokens] ..."

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

    @overload
    def call_llm[M: BaseModel](
        self,
        template: str | Prompt,
        *args: Any,
        specialty: TSpecialty = "general",
        model: TModel = "small",
        timeout: int = 180,
        transcript: bool | int | Literal["recent", "full"] = False,
        diff: bool | str = False,
        agent: bool = False,
        response_model: type[M],
        **kwargs: Any,
    ) -> M: ...

    @overload
    def call_llm(
        self,
        template: str | Prompt,
        *args: Any,
        specialty: TSpecialty = "general",
        model: TModel = "small",
        timeout: int = 180,
        transcript: bool | int | Literal["recent", "full"] = False,
        diff: bool | str = False,
        agent: bool = False,
        response_model: None = None,
        **kwargs: Any,
    ) -> str: ...

    def call_llm(
        self,
        template: str | Prompt,
        *args: Any,
        specialty: TSpecialty = "general",
        model: TModel = "small",
        timeout: int = 180,
        transcript: bool | int | Literal["recent", "full"] = False,
        diff: bool | str = False,
        agent: bool = False,
        response_model: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> str | BaseModel:
        diff_text = self.diff("uncommitted" if diff is True else diff) if diff else None
        if isinstance(template, Prompt):
            prompt = str(template.context("diff", diff_text))
            if transcript:
                prompt = f"{self.transcript_block(window=transcript_window(transcript))}\n\n<task>\n{prompt}\n</task>"
        else:
            block = self.transcript_block(window=transcript_window(transcript)) if transcript else ""
            if transcript:
                template = f"{{transcript}}\n\n<task>\n{template}\n</task>"
            prompt = template.format(*args, **kwargs, transcript=block)
            if diff_text is not None:
                prompt = f"<diff>\n{diff_text}\n</diff>\n\n{prompt}"
        backend = LlmBackends.for_specialty(specialty)
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("FACTORY_PROJECT_DIR")
        if response_model is not None:
            return extract_sync(
                prompt, response_model, backend=backend, model=model, agent=agent, cwd=cwd, timeout=timeout
            )
        return call_sync(prompt, backend=backend, model=model, agent=agent, cwd=cwd, timeout=timeout)
