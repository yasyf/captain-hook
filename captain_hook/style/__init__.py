from __future__ import annotations

import ast
import inspect
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from captain_hook.app import on
from captain_hook.state import hook_name
from captain_hook.style import matchers
from captain_hook.style.matchers import Matcher
from captain_hook.style.scope import changed_lines, read_source, reconstruct_pre
from captain_hook.style.types import StyleDiffRule, StyleRule, Violation
from captain_hook.types import Action, Event, FilePath, HookResult, TCondition, TestFile, Tool

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

logger = logging.getLogger(__name__)

GUARD_ONLY_IF: tuple[TCondition, ...] = (Tool("Edit|Write"), FilePath("*.py", project_only=False))
GUARD_SKIP_IF: tuple[TCondition, ...] = (TestFile(),)

__all__ = [
    "Matcher",
    "StyleDiffRule",
    "StyleRule",
    "Violation",
    "matchers",
    "styleguide",
]


def styleguide(
    *rules: type[StyleRule],
    block: bool = False,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    events: Event | None = None,
    max_shown: int = 5,
) -> None:
    """Register one change-scoped hook applying the given style rules to Python edits and writes.

    Each rule is a [`StyleRule`][captain_hook.style.StyleRule] (or
    [`StyleDiffRule`][captain_hook.style.StyleDiffRule]) subclass whose docstring is its
    message. The single registered hook parses the edited file once, runs every rule against the
    post-edit tree, scopes each violation to the changed lines, and emits one aggregated warning
    (or block, when ``block`` is set). Call again with different ``only_if`` / ``skip_if`` /
    ``events`` / ``block`` to register a separately scoped hook.

    Args:
        rules: ``StyleRule`` / ``StyleDiffRule`` subclasses to apply.
        block: Block the tool call instead of warning.
        only_if: Extra conditions ANDed onto the built-in ``Edit|Write`` + ``*.py`` guards.
        skip_if: Extra conditions ORed onto the built-in test-file skip.
        events: Override the default ``PostToolUse`` targeting.
        max_shown: Maximum violations shown per rule.

    Example:
        >>> styleguide(NoPrint, NoBareExcept)
        >>> styleguide(NoSqlInjection, block=True, only_if=[FilePath("api/**/*.py")])
    """
    if not (instances := [validate(rule)() for rule in rules]):
        return

    def handler(evt: BaseHookEvent) -> HookResult | None:
        try:
            return run_rules(instances, evt, block=block, max_shown=max_shown)
        except Exception:
            logger.warning("styleguide hook failed", exc_info=True)
            return None

    handler.__name__ = handler.__qualname__ = hook_name("styleguide", None, "+".join(r.slug() for r in instances))

    on(
        events or Event.PostToolUse,
        only_if=(*GUARD_ONLY_IF, *only_if),
        skip_if=(*GUARD_SKIP_IF, *skip_if),
        tests={key: expected for inst in instances for key, expected in type(inst).tests.items()},
    )(handler)


def validate(rule: object) -> type[StyleRule]:
    if not (isinstance(rule, type) and issubclass(rule, StyleRule)):
        raise TypeError(f"styleguide() expects StyleRule subclasses, got {rule!r}")
    if rule.__doc__ is None:
        raise ValueError(f"{rule.__name__} must define a docstring — it is the rule's message")
    base = StyleDiffRule if issubclass(rule, StyleDiffRule) else StyleRule
    if rule.match is None and rule.check is base.check:
        raise TypeError(f"{rule.__name__} must define `match` or override `check`")
    return rule


def run_rules(rules: list[StyleRule], evt: BaseHookEvent, *, block: bool, max_shown: int) -> HookResult | None:
    if (source := read_source(evt)) is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    pre = reconstruct_pre(evt, source)
    changed = changed_lines(pre, source)
    pre_tree = parse_quietly(pre) if any(isinstance(r, StyleDiffRule) for r in rules) else None
    if not (
        sections := [
            section
            for rule in rules
            if (section := run_one(rule, tree, pre_tree, changed, max_shown)) is not None
        ]
    ):
        return None
    return HookResult(action=Action.block if block else Action.warn, message="\n\n".join(sections))


def run_one(
    rule: StyleRule,
    tree: ast.Module,
    pre_tree: ast.Module | None,
    changed: set[int],
    max_shown: int,
) -> str | None:
    match rule:
        case StyleDiffRule() if pre_tree is not None:
            violations = rule.check(pre_tree, tree)
        case StyleDiffRule():
            return None
        case _:
            violations = rule.check(tree)
    if not (scoped := [v for v in violations if v.line in changed]):
        return None
    block = rule.sep.join(f"{v.label} (line {v.line})" for v in scoped[:max_shown])
    doc = inspect.cleandoc(type(rule).__doc__ or "")
    return doc.format(violations=block) if "{violations}" in doc else f"{doc}{rule.sep}{block}"


def parse_quietly(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None
