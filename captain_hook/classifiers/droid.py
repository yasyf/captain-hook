from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from captain_hook.transcript.models import TranscriptMessage


def classifier(msg: TranscriptMessage) -> bool:
    return msg.type == "user" and bool(msg.text.strip())


def detect(
    *,
    cwd: str | None = None,
    transcript_path: str | None = None,
    messages: list[TranscriptMessage] | None = None,
) -> bool:
    return "FACTORY_PROJECT_DIR" in os.environ
