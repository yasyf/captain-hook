from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest

from captain_hook.snapshots.schema import compose

REPO = Path(__file__).parent.parent


@pytest.mark.parametrize("directory", ["captain_hook/snapshots/schemas", "internal/snapshots/schema"])
def test_the_vendored_schemas_match_the_pinned_cc_transcript(directory: str) -> None:
    core = Path(str(files("cc_transcript").joinpath("snapshot_schema")))
    for name, schema in compose(core).items():
        vendored = REPO / directory / f"{name}.schema.json"
        assert vendored.read_text() == json.dumps(schema, indent=2, sort_keys=True) + "\n", (
            f"{vendored} is stale for this cc-transcript: run `python -m captain_hook.snapshots.schema {directory}`"
        )
