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
from pathlib import Path

import pytest

import captain_hook
from captain_hook import Event
from captain_hook.dispatch import dispatch
from captain_hook.hook_lint import lint_paths
from captain_hook.util.builds import LOAD_PER_CORE
from tests.helpers import make_pre_tool_event

BUILTIN_PACKS_DIR = Path(captain_hook.__file__).parent / "builtin_packs"


def builtin_hook_modules() -> list[tuple[str, str]]:
    """Every builtin-pack hook module as ``(dotted_name, id)``, ``_``-prefixed files skipped, name-ordered."""
    return [
        (f"captain_hook.builtin_packs.{(pack := path.parent.parent.name)}.hooks.{path.stem}", f"{pack}/{path.stem}")
        for path in sorted(BUILTIN_PACKS_DIR.glob("*/hooks/*.py"))
        if not path.stem.startswith("_")
    ]


MODULES = builtin_hook_modules()


def load_builtin_module(dotted: str) -> None:
    # Register into the reset _state: reload re-runs the body when a prior test imported the module,
    # since a plain import_module would return the cached object without re-firing its hook() calls.
    if (module := sys.modules.get(dotted)) is not None:
        importlib.reload(module)
    else:
        importlib.import_module(dotted)


def test_builtin_packs_meet_the_authoring_bar() -> None:
    assert lint_paths([BUILTIN_PACKS_DIR]) == []


@pytest.mark.parametrize("dotted", [dotted for dotted, _ in MODULES], ids=[name for _, name in MODULES])
def test_builtin_pack_inline_tests(dotted: str) -> None:
    from tests.helpers import assert_inline_tests

    load_builtin_module(dotted)

    assert_inline_tests(dotted)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("go build ./next/...", id="bare-wildcard-build"),
        pytest.param(
            "/Users/dev/.local/share/mise/installs/go/1.26.9/bin/go build ./...", id="absolute-wildcard-build"
        ),
        pytest.param("go vet ./next/internal/iris/", id="bare-vet"),
        pytest.param(
            "env CGO_ENABLED=1 timeout 600 /Users/dev/.local/share/mise/installs/go/1.26.9/bin/go vet ./...",
            id="absolute-cgo-vet",
        ),
    ],
)
def test_pressure_admission_blocks_repeated_local_builds(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LOAD_PER_CORE, "0")
    load_builtin_module("captain_hook.builtin_packs.performance.hooks.admission")

    for _ in range(2):
        result = dispatch(Event.PreToolUse, make_pre_tool_event(tool_input={"command": command}), session_dir=tmp_path)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "load gate" in result["hookSpecificOutput"]["permissionDecisionReason"]
