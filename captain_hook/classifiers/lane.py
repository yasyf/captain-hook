from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from cc_transcript.discovery import is_subagent_path
from cc_transcript.models import UserEvent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cc_transcript.models import TranscriptEvent


def classifier(event: UserEvent) -> bool:
    return not (event.meta.is_meta or event.meta.is_compact_summary or event.interrupted) and bool(event.text.strip())


def detect(
    *,
    cwd: str | None = None,
    transcript_path: str | None = None,
    events: Sequence[TranscriptEvent] | None = None,
) -> bool:
    if transcript_path and is_subagent_path(Path(transcript_path)):
        return True
    if events:
        return any(isinstance(event, UserEvent) for event in events) and all(
            event.meta.is_sidechain for event in events if isinstance(event, UserEvent)
        )
    return False
