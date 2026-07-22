"""Per-file gate: every builtin-pack hook module's inline ``tests={...}`` pass through the real engine.

One pytest item per ``captain_hook/builtin_packs/*/hooks/*.py`` file (``_``-prefixed libraries skipped),
enumerated at collection time. The body loads exactly that module so its ``hook()``/``@on`` calls register
into the freshly-reset ``_state`` (the autouse ``clean_state`` fixture brackets each test with ``reset()``),
then runs :func:`~captain_hook.testing.helpers.run_inline_tests` over that registration set. A module with
no ``tests=`` hooks yields an empty result and passes trivially. This is the baseline gate for the wave that
migrates the builtin corpus, so it must stay green against every shipped hook file.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import captain_hook

BUILTIN_PACKS_DIR = Path(captain_hook.__file__).parent / "builtin_packs"


def builtin_hook_modules() -> list[tuple[str, str]]:
    """Every builtin-pack hook module as ``(dotted_name, id)``, ``_``-prefixed files skipped, name-ordered."""
    return [
        (f"captain_hook.builtin_packs.{(pack := path.parent.parent.name)}.hooks.{path.stem}", f"{pack}/{path.stem}")
        for path in sorted(BUILTIN_PACKS_DIR.glob("*/hooks/*.py"))
        if not path.stem.startswith("_")
    ]


MODULES = builtin_hook_modules()


def dispatch_comment_case(content_name: str, file_name: str, *, advisory_on_deny: bool) -> str | None:
    from captain_hook.app import _state
    from captain_hook.app import hook as register_hook
    from captain_hook.dispatch import dispatch
    from captain_hook.testing.helpers import input_to_event
    from captain_hook.testing.types import FileFixture, Input
    from captain_hook.types import Event

    register_hook(Event.PreToolUse, "blocked first", block=True)
    dotted = "captain_hook.builtin_packs.general.hooks.comments"
    module = (
        importlib.reload(loaded) if (loaded := sys.modules.get(dotted)) is not None else importlib.import_module(dotted)
    )
    if not advisory_on_deny:
        _state.hooks[:] = [
            replace(entry, spec=replace(entry.spec, advisory_on_deny=False)) if entry.spec.advisory_on_deny else entry
            for entry in _state.hooks
        ]
    evt = input_to_event(
        Event.PreToolUse,
        Input(tool="Write", file=FileFixture(name=file_name, content=""), content=getattr(module, content_name)),
    )
    result = dispatch(Event.PreToolUse, evt)
    assert result is not None
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("dotted", [dotted for dotted, _ in MODULES], ids=[name for _, name in MODULES])
def test_builtin_pack_inline_tests(dotted: str) -> None:
    from tests.helpers import assert_inline_tests

    # Register into the reset _state: reload re-runs the body when a prior test imported the module,
    # since a plain import_module would return the cached object without re-firing its hook() calls.
    if (module := sys.modules.get(dotted)) is not None:
        importlib.reload(module)
    else:
        importlib.import_module(dotted)

    assert_inline_tests(dotted)


@pytest.mark.parametrize(
    ("content_name", "file_name", "message"),
    [
        pytest.param("RS_LONG_DOC", "lib.rs", "Long documentation comment", id="verbose-doc"),
        pytest.param("PY_DENSE_FIRES", "dense.py", "Comment-dense edit", id="comment-dense"),
    ],
)
def test_general_comment_advisories_cofire_after_deny(content_name: str, file_name: str, message: str) -> None:
    reason = dispatch_comment_case(content_name, file_name, advisory_on_deny=True)
    assert reason is not None
    assert "blocked first" in reason
    assert "Additional advisories (not the reason for the deny):" in reason
    assert message in reason


@pytest.mark.parametrize(
    ("content_name", "file_name", "message"),
    [
        pytest.param("RS_LONG_DOC", "lib.rs", "Long documentation comment", id="verbose-doc"),
        pytest.param("PY_DENSE_FIRES", "dense.py", "Comment-dense edit", id="comment-dense"),
    ],
)
def test_general_comment_advisories_require_opt_in(content_name: str, file_name: str, message: str) -> None:
    reason = dispatch_comment_case(content_name, file_name, advisory_on_deny=False)
    assert reason == "blocked first"
    assert message not in reason
