from __future__ import annotations

import ast
import sys
import traceback
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, get_type_hints, overload

from captain_hook.app import get_current_app
from captain_hook.state import hook_name
from captain_hook.types import (
    Action,
    Event,
    FilePath,
    HookResult,
    TCondition,
    TestFile,
    Tool,
    TTest,
)

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

StringCheck = Callable[[str], list[str]]
AstCheck = Callable[[ast.AST], Iterator[str]]

DEFAULT_ONLY_IF: tuple[TCondition, ...] = (Tool("Edit|Write"), FilePath("*.py"))
DEFAULT_SKIP_IF: tuple[TCondition, ...] = (TestFile(),)


def detect_ast_mode(check: StringCheck | AstCheck) -> bool:
    hints = get_type_hints(check)
    first_param = next(iter(hints.values()), None)
    return first_param is ast.AST or (isinstance(first_param, type) and issubclass(first_param, ast.AST))


def read_source(evt: BaseHookEvent) -> str | None:
    if evt.file and evt.file.path.exists():
        try:
            return evt.file.read_text()
        except (OSError, UnicodeDecodeError):
            pass
    return evt.content


@overload
def lint(
    check: Callable[[str], list[str]],
    *,
    message: str,
    trigger: str | None = ...,
    sep: str = ...,
    block: bool = ...,
    events: Event | None = ...,
    tests: TTest | None = ...,
    max_shown: int = ...,
) -> None: ...


@overload
def lint(
    check: Callable[[ast.AST], Iterator[str]],
    *,
    message: str,
    trigger: str | None = ...,
    sep: str = ...,
    block: bool = ...,
    events: Event | None = ...,
    tests: TTest | None = ...,
    max_shown: int = ...,
) -> None: ...


def lint(
    check: Callable[[str], list[str]] | Callable[[ast.AST], Iterator[str]],
    *,
    message: str,
    trigger: str | None = None,
    sep: str = ", ",
    block: bool = False,
    events: Event | None = None,
    tests: TTest | None = None,
    max_shown: int = 5,
) -> None:
    """Register a lint check that runs on Python file edits/writes.

    Supports two modes based on the ``check`` function's type hint:
    - **String mode**: receives the file content as ``str``, returns violation strings.
    - **AST mode**: receives each ``ast.AST`` node, yields violation strings.

    Automatically targets ``PostToolUse`` on ``Edit|Write`` of ``*.py``, skipping test files.

    Args:
        check: Lint function (string→list or AST node→iterator of violations).
        message: Format string for the result; use ``{violations}`` placeholder.
        trigger: Fast-path string check — skip AST parsing if absent from source.
        sep: Separator for joining violations (default ``", "``).
        block: If True, violations block instead of warn.
        events: Override default event targeting.
        tests: Inline test dict for ``run_inline_tests``.
        max_shown: Maximum violations to include in the message (default 5).

    Example:
        >>> def find_prints(content: str) -> list[str]:
        ...     return [line for line in content.splitlines() if "print(" in line]
        >>> lint(find_prints, message="Remove print statements: {violations}")
    """
    is_ast = detect_ast_mode(check)

    def handler(evt: BaseHookEvent) -> HookResult | None:
        try:
            if is_ast:
                return run_ast_check(check, evt, message, trigger, sep, block, max_shown)  # type: ignore[arg-type]
            return run_string_check(check, evt, message, sep, block, max_shown)  # type: ignore[arg-type]
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return None

    handler.__name__ = handler.__qualname__ = hook_name("lint", None, message)

    app = get_current_app()
    app.on(
        events or Event.PostToolUse,
        only_if=DEFAULT_ONLY_IF,
        skip_if=DEFAULT_SKIP_IF,
        tests=tests,
    )(handler)


def run_string_check(
    check: StringCheck,
    evt: BaseHookEvent,
    message: str,
    sep: str,
    block: bool,
    max_shown: int,
) -> HookResult | None:
    if (content := evt.content) is None:
        return None
    violations = check(content)
    return format_result(violations, message, sep, block, max_shown)


def run_ast_check(
    check: AstCheck,
    evt: BaseHookEvent,
    message: str,
    trigger: str | None,
    sep: str,
    block: bool,
    max_shown: int,
) -> HookResult | None:
    source = read_source(evt)
    if source is None:
        return None
    if trigger and trigger not in source:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    violations = [v for node in ast.walk(tree) for v in check(node)]
    return format_result(violations, message, sep, block, max_shown)


def format_result(
    violations: list[str],
    message: str,
    sep: str,
    block: bool,
    max_shown: int,
) -> HookResult | None:
    if not violations:
        return None
    formatted = sep.join(violations[:max_shown])
    return HookResult(
        action=Action.block if block else Action.warn,
        message=message.format(violations=formatted),
    )
