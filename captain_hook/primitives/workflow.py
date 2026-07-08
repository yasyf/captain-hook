from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar

from cc_transcript.models import AssistantEvent, UserEvent
from pydantic import BaseModel

from captain_hook.types import Action, Event, HookResult, InlineTests, TCondition, Waiting

if TYPE_CHECKING:
    from cc_transcript.query import Session

    from captain_hook.events import BaseHookEvent

M = TypeVar("M", bound=BaseModel)


def session_text(t: Session) -> str:
    return "\n".join(event.text for event in t.events if isinstance(event, UserEvent | AssistantEvent))


def text_matches(pattern: str) -> Callable[[Session], bool]:
    return lambda t: bool(re.search(pattern, session_text(t)))


@dataclass(frozen=True, kw_only=True)
class Step:
    """One step of a :func:`workflow` guard: ``check`` gates progress, ``message`` is shown when it fails.

    ``message`` is the full "you are here, do this next" sentence surfaced on the blocking
    guard. ``name`` is optional and used only for readability at the call site.

    Example:
        >>> Step(check=text_matches(r"pytest"), message="Step 1: run the tests, then fix failures.")
    """

    check: Callable[[Session], bool]
    message: str
    name: str = ""


@dataclass(frozen=True, kw_only=True)
class Artifact(Generic[M]):  # noqa: UP046
    path: str
    model: type[M]
    validate: Callable[[M], str | None] = lambda _: None


@dataclass(frozen=True, kw_only=True)
class Workflow:
    label: str
    marker: str
    steps: list[Step]
    artifacts: list[Artifact[BaseModel]] = field(default_factory=list)
    post_complete: Callable[[BaseHookEvent], HookResult | None] | None = None
    on_start: Callable[[BaseHookEvent], HookResult | None] | None = None

    def setup(self, evt: BaseHookEvent) -> HookResult | None:
        """Run the ``on_start`` callback when the workflow's subagent launches."""
        return self.on_start(evt) if self.on_start else None

    def guard(self, evt: BaseHookEvent) -> HookResult | None:
        if self.marker not in session_text(evt.ctx.t):
            resume = next(
                (s for s in self.steps if not s.check(evt.ctx.t)),
                self.steps[-1],
            )
            return HookResult(
                action=Action.block,
                message=f"{self.label} INCOMPLETE: {resume.message}",
            )

        for art in self.artifacts:
            if not (p := Path(art.path)).exists():
                return HookResult(
                    action=Action.block,
                    message=f"{self.label} INCOMPLETE: {art.path} not found.",
                )
            try:
                parsed = art.model.model_validate_json(p.read_text())
            except (ValueError, OSError) as exc:
                return HookResult(
                    action=Action.block,
                    message=f"{self.label} INCOMPLETE: {art.path} invalid: {exc}",
                )
            if err := art.validate(parsed):
                return HookResult(action=Action.block, message=f"{self.label} INCOMPLETE: {err}")

        return self.post_complete(evt) if self.post_complete else None


def workflow(
    *,
    label: str,
    marker: str,
    steps: list[Step],
    artifacts: list[Artifact[BaseModel]] | None = None,
    post_complete: Callable[[BaseHookEvent], HookResult | None] | None = None,
    on_start: Callable[[BaseHookEvent], HookResult | None] | None = None,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    tests: InlineTests | None = None,
) -> None:
    from captain_hook.app import on

    w = Workflow(
        label=label,
        marker=marker,
        steps=steps,
        artifacts=artifacts or [],
        post_complete=post_complete,
        on_start=on_start,
    )

    def guard(evt: BaseHookEvent) -> HookResult | None:
        return w.guard(evt)

    guard.__name__ = f"{label.lower().replace('-', '_')}_workflow_guard"
    guard.__qualname__ = guard.__name__
    on(Event.SubagentStop, only_if=only_if, skip_if=(Waiting(), *skip_if), max_fires=1, tests=tests)(guard)

    if on_start is not None:

        def setup(evt: BaseHookEvent) -> HookResult | None:
            return w.setup(evt)

        setup.__name__ = f"{label.lower().replace('-', '_')}_workflow_setup"
        setup.__qualname__ = setup.__name__
        on(Event.SubagentStart, only_if=only_if, skip_if=skip_if)(setup)
