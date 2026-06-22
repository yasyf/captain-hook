from __future__ import annotations

from typing import TYPE_CHECKING

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
