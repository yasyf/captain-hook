from __future__ import annotations

from typing import TYPE_CHECKING, Any

from captain_hook import ast_grep
from captain_hook.app import on
from captain_hook.state import hook_name
from captain_hook.types import Event, Tool

if TYPE_CHECKING:
    from collections.abc import Sequence

    from captain_hook.events import PreToolUseEvent
    from captain_hook.types import HookResponse, InlineTests, TCondition


def rewrite_code(
    pattern: str,
    replace: str,
    *,
    lang: str | None = None,
    only_if: Sequence[TCondition] = (),
    note: str | None = None,
    project_only: bool = True,
    tests: InlineTests | None = None,
) -> None:
    """Register a ``PreToolUse`` hook that structurally rewrites edited code before it is written.

    The code counterpart to [`rewrite_command`][captain_hook.rewrite_command]: every ast-grep
    ``pattern`` match in the edit's new content is rewritten to ``replace``, an ast-grep ``$VAR`` /
    ``$$$VAR`` fix template. It applies across Edit, Write, MultiEdit, and NotebookEdit, and is
    idempotent — when nothing matches, the tool passes through untouched. ``note`` surfaces as
    ``additionalContext`` so the model sees that its input was rewritten.

    Args:
        pattern: The ast-grep pattern to match, e.g. ``"os.system($CMD)"``.
        replace: The ast-grep fix template, e.g. ``"subprocess.run([$CMD], check=True)"``.
        lang: ast-grep language id (``"py"``, ``"ts"``, ``"tsx"``, ``"js"``, ``"jsx"``, ``"go"``,
            ``"rs"``, ``"java"``, ``"bash"``); inferred from the edited file's extension when omitted.
            Pass it explicitly for an extension that carries no language, like a notebook's ``.ipynb``.
        only_if: Extra conditions ANDed onto the built-in editing-tool guard.
        note: Advisory context surfaced alongside the rewrite.
        project_only: Only rewrite files inside the repository root (default ``True``).
        tests: Inline tests for the registered hook.

    Example:
        >>> rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)", note="Use subprocess.run")
    """

    def handler(evt: PreToolUseEvent) -> HookResponse:
        from captain_hook.conditions import is_project_file

        if not evt.file or (project_only and not is_project_file(evt)):
            return None
        if not (resolved := lang or ast_grep.lang_for_path(evt.file.path)):
            return None

        def transform(source: str) -> str:
            return ast_grep.rewrite(source, resolved, pattern, replace)

        return evt.rewrite_content(transform, note=note)

    handler.__name__ = handler.__qualname__ = hook_name("rewrite_code", None, f"{pattern}=>{replace}")
    on(Event.PreToolUse, only_if=[Tool("Edit|Write|MultiEdit|NotebookEdit"), *only_if], tests=tests)(handler)


def set_tool_input(
    field: str,
    value: Any,
    *,
    tool: str,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    note: str | None = None,
    tests: InlineTests | None = None,
) -> None:
    """Register a ``PreToolUse`` hook that fills a MISSING top-level input field with ``value``.

    When ``field`` is absent from the matched tool's input, the input is rewritten to
    ``{**raw, field: value}`` and allowed, with ``note`` surfaced as ``additionalContext``. A field
    already present — even falsy — is left untouched, so an explicit choice is never clobbered.

    Args:
        field: The top-level tool-input key to fill.
        value: The value to set when the field is absent.
        tool: Tool-name pattern to gate on, e.g. ``"Agent|Task"``.
        only_if: Extra conditions ANDed onto the built-in tool guard.
        skip_if: Conditions that skip the hook when any matches.
        note: Advisory context surfaced alongside the rewrite.
        tests: Inline tests for the registered hook.

    Example:
        >>> set_tool_input("model", "sonnet", tool="Agent|Task", only_if=[Agent("Explore")], note="upgraded")
    """

    def handler(evt: PreToolUseEvent) -> HookResponse:
        raw = evt.input.raw
        return None if field in raw else evt.rewrite({**raw, field: value}, note=note)

    handler.__name__ = handler.__qualname__ = hook_name("set_tool_input", None, f"{tool}:{field}")
    on(Event.PreToolUse, only_if=[Tool(tool), *only_if], skip_if=skip_if, tests=tests)(handler)
