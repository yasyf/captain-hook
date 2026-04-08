from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from captain_hook.transcript.models import TranscriptMessage

SYNTHETIC_PREFIXES = (
    "<system_instruction>",
    "<task-notification>",
    "<local-command-caveat>",
    "<command-name>",
)


def classifier(msg: TranscriptMessage) -> bool:
    if msg.type != "user":
        return False
    text = msg.text.strip()
    return bool(text) and not text.startswith(SYNTHETIC_PREFIXES)


def detect(
    *,
    cwd: str | None = None,
    transcript_path: str | None = None,
    messages: list[TranscriptMessage] | None = None,
) -> bool:
    if cwd and "conductor/workspaces" in cwd:
        return True
    if transcript_path and "conductor-workspaces" in transcript_path:
        return True
    if messages:
        return any(m.type == "user" and m.text.strip().startswith("<system_instruction>") for m in messages[:50])
    return False
