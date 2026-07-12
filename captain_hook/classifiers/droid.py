from __future__ import annotations

from typing import TYPE_CHECKING

from cc_transcript.activity import native_user_classifier

from captain_hook.util import reqenv

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
    return reqenv.getenv("FACTORY_PROJECT_DIR") is not None
