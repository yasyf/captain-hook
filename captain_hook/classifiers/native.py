from __future__ import annotations

from typing import TYPE_CHECKING

from cc_transcript.activity import native_user_classifier

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cc_transcript.models import TranscriptEvent

classifier = native_user_classifier


def detect(
    *,
    cwd: str | None = None,
    transcript_path: str | None = None,
    events: Sequence[TranscriptEvent] | None = None,
) -> bool:
    return True
