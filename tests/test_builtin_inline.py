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

BUILTIN_PACKS_DIR = Path(captain_hook.__file__).parent / "builtin_packs"


def builtin_hook_modules() -> list[tuple[str, str]]:
    """Every builtin-pack hook module as ``(dotted_name, id)``, ``_``-prefixed files skipped, name-ordered."""
    return [
        (f"captain_hook.builtin_packs.{(pack := path.parent.parent.name)}.hooks.{path.stem}", f"{pack}/{path.stem}")
        for path in sorted(BUILTIN_PACKS_DIR.glob("*/hooks/*.py"))
        if not path.stem.startswith("_")
    ]


MODULES = builtin_hook_modules()


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
