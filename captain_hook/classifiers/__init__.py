from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cc_transcript.activity import UserClassifier
    from cc_transcript.models import TranscriptEvent


def detect(
    cwd: str | None = None,
    transcript_path: str | None = None,
    events: Sequence[TranscriptEvent] | None = None,
) -> UserClassifier:
    """Auto-detect the environment and return the user classifier for turn segmentation."""
    return next(
        mod.classifier
        for name in ("lane", "droid", "conductor", "native")
        if (mod := importlib.import_module(f".{name}", __package__))
        and mod.detect(cwd=cwd, transcript_path=transcript_path, events=events)
    )
