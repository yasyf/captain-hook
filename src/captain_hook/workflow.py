from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar

from pydantic import BaseModel

from captain_hook.types import Action, Event, HookResult, TCondition, TTest

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent
    from captain_hook.transcript import Transcript

M = TypeVar("M", bound=BaseModel)


def text_matches(pattern: str) -> Callable[[Transcript], bool]:
    return lambda t: bool(re.search(pattern, t.full_text))


@dataclass(frozen=True, kw_only=True)
class Step:
    name: str
    check: Callable[[Transcript], bool]
    stopped_at: str
    next_step: str


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
    artifacts: list[Artifact[BaseModel]] = field(default_factory=lambda: [])
    post_complete: Callable[[BaseHookEvent], HookResult | None] | None = None

    def guard(self, evt: BaseHookEvent) -> HookResult | None:
        if self.marker not in evt.ctx.t.full_text:
            resume = next(
                (s for s in self.steps if not s.check(evt.ctx.t)),
                self.steps[-1],
            )
            return HookResult(
                action=Action.block,
                message=f"{self.label} INCOMPLETE: {resume.stopped_at} {resume.next_step}",
            )

        for art in self.artifacts:
            if not (p := Path(art.path)).exists():
                return HookResult(
                    action=Action.block,
                    message=f"{self.label} INCOMPLETE: {art.path} not found.",
                )
            try:
                parsed = art.model.model_validate_json(p.read_text())
            except Exception as exc:
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
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    tests: TTest | None = None,
) -> None:
    from captain_hook.app import on

    w = Workflow(
        label=label,
        marker=marker,
        steps=steps,
        artifacts=artifacts or [],
        post_complete=post_complete,
    )

    def guard(evt: BaseHookEvent) -> HookResult | None:
        return w.guard(evt)

    guard.__name__ = f"{label.lower().replace('-', '_')}_workflow_guard"
    guard.__qualname__ = guard.__name__
    on(Event.SubagentStop, only_if=only_if, skip_if=skip_if, max_fires=1, tests=tests)(guard)
