from __future__ import annotations

from typing import TYPE_CHECKING

from cc_transcript.activity import native_user_classifier
from cc_transcript.filterspec import STRUCTURAL_NOISE_RE
from cc_transcript.models import UserEvent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cc_transcript.models import TranscriptEvent


def classifier(event: UserEvent) -> bool:
    return native_user_classifier(event) and STRUCTURAL_NOISE_RE.match(event.text.strip()) is None


def detect(
    *,
    cwd: str | None = None,
    transcript_path: str | None = None,
    events: Sequence[TranscriptEvent] | None = None,
) -> bool:
    if cwd and "conductor/workspaces" in cwd:
        return True
    if transcript_path and "conductor-workspaces" in transcript_path:
        return True
    if events:
        return any(
            isinstance(event, UserEvent) and event.text.strip().startswith("<system_instruction>")
            for event in events[:50]
        )
    return False
