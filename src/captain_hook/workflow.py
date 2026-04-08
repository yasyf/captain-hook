from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar

from pydantic import BaseModel

from captain_hook.types import Action, Event, HookResult, TTest

if TYPE_CHECKING:
    from captain_hook.app import HookApp
    from captain_hook.events import BaseHookEvent
    from captain_hook.transcript import Transcript

M = TypeVar("M", bound=BaseModel)


def text_matches(pattern: str) -> Callable[[Transcript], bool]:
    """Create a transcript predicate that checks ``full_text`` against a regex pattern.

    Args:
        pattern: Regex pattern to search for.

    Returns:
        Callable that returns True if the pattern is found in the transcript.
    """
    return lambda t: bool(re.search(pattern, t.full_text))


@dataclass(frozen=True, kw_only=True)
class Step:
    """A single step in a workflow: a transcript predicate with gate message when incomplete."""

    name: str
    check: Callable[[Transcript], bool]
    stopped_at: str
    next_step: str


@dataclass(frozen=True, kw_only=True)
class Artifact(Generic[M]):  # noqa: UP046
    """A file artifact that must exist and validate after workflow completion."""

    path: str
    model: type[M]
    validate: Callable[[M], str | None] = lambda _: None


@dataclass(frozen=True, kw_only=True)
class Workflow:
    """A multi-step guard that blocks until all steps pass, a marker appears, and artifacts validate."""

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
    app: HookApp | None = None,
    label: str,
    marker: str,
    steps: list[Step],
    artifacts: list[Artifact[BaseModel]] | None = None,
    post_complete: Callable[[BaseHookEvent], HookResult | None] | None = None,
    tests: TTest | None = None,
) -> Workflow:
    """Register a multi-step workflow guard on ``SubagentStop``.

    The workflow blocks until all steps pass their ``check`` predicates,
    the ``marker`` text appears in the transcript, and all artifacts exist
    and validate. Fires at most once per session.

    Args:
        app: HookApp to register on (defaults to current app).
        label: Display label for block messages (e.g. ``"CLEANUP"``).
        marker: Text that must appear in ``full_text`` to indicate completion.
        steps: Ordered list of ``Step`` objects with transcript predicates.
        artifacts: Files that must exist and validate after completion.
        post_complete: Optional handler called when the workflow passes.
        tests: Inline test dict for ``run_inline_tests``.

    Returns:
        The constructed Workflow instance.

    Example:
        >>> workflow(label="CLEANUP", marker="CLEANUP COMPLETE",
        ...          steps=[Step(name="run tests", check=text_matches(r"mtest"),
        ...                      stopped_at="Stop here.", next_step="Run tests.")])
    """
    from captain_hook.app import get_current_app

    if app is None:
        app = get_current_app()

    w = Workflow(
        label=label,
        marker=marker,
        steps=steps,
        artifacts=artifacts or [],
        post_complete=post_complete,
    )
    app.on(Event.SubagentStop, max_fires=1, tests=tests)(w.guard)
    return w
