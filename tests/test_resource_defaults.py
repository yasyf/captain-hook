from __future__ import annotations

import json
from pathlib import Path

from captain_hook import resource_defaults
from hatch_build import RESOURCE_HEADER, render_resource_literal

ROOT = Path(__file__).parents[1]
DEFINITION = json.loads((ROOT / "internal" / "wireproto" / "resource.json").read_text())


def test_tracked_rendering_is_the_definition_rendered() -> None:
    rendered = render_resource_literal(DEFINITION)
    assert rendered.startswith(RESOURCE_HEADER)
    assert (ROOT / "captain_hook" / "resource_defaults.py").read_text() == rendered


def test_literal_carries_the_definition_values() -> None:
    assert resource_defaults.ENV_PREFIX == DEFINITION["env_prefix"]
    assert resource_defaults.DEFAULTS == DEFINITION["defaults"]
    assert list(resource_defaults.DEFAULTS) == list(DEFINITION["defaults"])
