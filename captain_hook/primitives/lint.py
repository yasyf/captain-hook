from __future__ import annotations

import ast
import textwrap
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, get_type_hints, overload

from loguru import logger

from captain_hook.app import on
from captain_hook.state import hook_name
from captain_hook.types import (
    Action,
    Event,
    FilePath,
    HookResult,
    InlineTests,
    SourceEdits,
    TCondition,
    TestFile,
    Tool,
)

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

StringCheck = Callable[[str], list[str]]
AstCheck = Callable[[ast.AST], Iterator[str]]
DiffCheck = Callable[[ast.AST, ast.AST], list[str]]

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
    lang: str = ...,
    trigger: str | None = ...,
    sep: str = ...,
    block: bool = ...,
    events: Event | None = ...,
    tests: InlineTests | None = ...,
    max_shown: int = ...,
) -> None: ...


@overload
def lint(
    check: Callable[[ast.AST], Iterator[str]],
    *,
    message: str,
    lang: str = ...,
    trigger: str | None = ...,
    sep: str = ...,
    block: bool = ...,
    events: Event | None = ...,
    tests: InlineTests | None = ...,
    max_shown: int = ...,
) -> None: ...


@overload
def lint(
    *,
    pattern: str,
    message: str,
    lang: str = ...,
    sep: str = ...,
    block: bool = ...,
    events: Event | None = ...,
    tests: InlineTests | None = ...,
    max_shown: int = ...,
) -> None: ...


def lint(
    check: Callable[[str], list[str]] | Callable[[ast.AST], Iterator[str]] | None = None,
    *,
    pattern: str | None = None,
    message: str,
    lang: str = "py",
    trigger: str | None = None,
    sep: str = ", ",
    block: bool = False,
    events: Event | None = None,
    tests: InlineTests | None = None,
    max_shown: int = 5,
) -> None:
    """Register a lint check that runs on source-file edits and writes.

    Three modes, exactly one of ``check`` or ``pattern``:
    - **String mode**: ``check`` receives the file content as ``str``, returns violation strings.
    - **AST mode**: ``check`` receives each ``ast.AST`` node, yields violation strings (Python only).
    - **ast-grep mode**: ``pattern`` is an ast-grep pattern string; every structural match becomes a
      ``"{snippet} (line N)"`` violation. ``lang`` (default ``"py"``) selects the language *and* the
      file guard, so a ``lang="ts"`` lint fires on ``*.ts`` edits.

    Example:
        >>> def find_prints(content: str) -> list[str]:
        ...     return [line for line in content.splitlines() if "print(" in line]
        >>> lint(find_prints, message="Remove print statements: {violations}")
        >>> lint(pattern="console.log($$$)", message="Drop console.log: {violations}", lang="ts")
    """
    if pattern is not None:
        if check is not None:
            raise TypeError("lint takes either a check function or pattern=, not both or neither")

        def pattern_handler(evt: BaseHookEvent) -> HookResult | None:
            from captain_hook.ast_grep import find_all

            if (content := evt.content) is None:
                return None
            try:
                violations = [f"{m.text.splitlines()[0]} (line {m.line})" for m in find_all(content, lang, pattern)]
            except Exception:
                logger.bind(pattern=pattern).opt(exception=True).warning("lint pattern check failed")
                return None
            return format_result(violations, message, sep, block, max_shown)

        pattern_handler.__name__ = pattern_handler.__qualname__ = hook_name("lint", None, message)
        on(
            events or Event.PostToolUse,
            only_if=(Tool("Edit|Write"), FilePath(*SourceEdits(lang=lang).globs, project_only=False)),
            skip_if=DEFAULT_SKIP_IF,
            tests=tests,
        )(pattern_handler)
        return

    if check is None:
        raise TypeError("lint takes either a check function or pattern=, not both or neither")

    is_ast = detect_ast_mode(check)

    def handler(evt: BaseHookEvent) -> HookResult | None:
        try:
            if is_ast:
                return run_ast_check(check, evt, message, trigger, sep, block, max_shown)  # type: ignore[arg-type]
            return run_string_check(check, evt, message, trigger, sep, block, max_shown)  # type: ignore[arg-type]
        except Exception:
            logger.bind(check=check.__name__).opt(exception=True).warning("lint check failed")
            return None

    handler.__name__ = handler.__qualname__ = hook_name("lint", None, message)

    on(
        events or Event.PostToolUse,
        only_if=(Tool("Edit|Write"), FilePath(*SourceEdits(lang=lang).globs, project_only=False)),
        skip_if=DEFAULT_SKIP_IF,
        tests=tests,
    )(handler)


def diff_lint(
    check: DiffCheck,
    *,
    message: str,
    sep: str = ", ",
    block: bool = False,
    events: Event | None = None,
    tests: InlineTests | None = None,
    max_shown: int = 5,
    only_if: Sequence[TCondition] = (Tool("Edit"), FilePath("*.py", project_only=False)),
    skip_if: Sequence[TCondition] = DEFAULT_SKIP_IF,
) -> None:
    def handler(evt: BaseHookEvent) -> HookResult | None:
        try:
            return run_diff_check(check, evt, message, sep, block, max_shown)
        except Exception:
            logger.bind(check=check.__name__).opt(exception=True).warning("diff lint check failed")
            return None

    handler.__name__ = handler.__qualname__ = hook_name("diff_lint", None, message)

    on(
        events or Event.PostToolUse,
        only_if=only_if,
        skip_if=skip_if,
        tests=tests,
    )(handler)


def run_string_check(
    check: StringCheck,
    evt: BaseHookEvent,
    message: str,
    trigger: str | None,
    sep: str,
    block: bool,
    max_shown: int,
) -> HookResult | None:
    if (content := evt.content) is None:
        return None
    if trigger and trigger not in content:
        return None
    return format_result(check(content), message, sep, block, max_shown)


def run_diff_check(
    check: DiffCheck,
    evt: BaseHookEvent,
    message: str,
    sep: str,
    block: bool,
    max_shown: int,
) -> HookResult | None:
    if (old := evt.old) is None or (new := evt.content) is None:
        return None
    try:
        old_tree = ast.parse(textwrap.dedent(old))
        new_tree = ast.parse(textwrap.dedent(new))
    except SyntaxError:
        return None
    return format_result(check(old_tree, new_tree), message, sep, block, max_shown)


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
    shown = violations[:max_shown]
    formatted = sep.join((*shown, f"…(+{len(violations) - max_shown} more)") if len(violations) > max_shown else shown)
    return HookResult(
        action=Action.block if block else Action.warn,
        message=message.format(violations=formatted),
    )
