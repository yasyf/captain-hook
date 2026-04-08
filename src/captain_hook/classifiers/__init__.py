from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from captain_hook.transcript.models import TranscriptMessage


@runtime_checkable
class MessageClassifier(Protocol):
    """Callable that returns True for messages classified as real user messages."""

    def __call__(self, msg: TranscriptMessage) -> bool: ...

CLASSIFIER_MODULES = ("droid", "conductor", "native")


def detect(
    cwd: str | None = None,
    transcript_path: str | None = None,
    messages: list[TranscriptMessage] | None = None,
) -> MessageClassifier:
    """Auto-detect the environment and return the appropriate message classifier."""
    return next(
        mod.classifier
        for name in CLASSIFIER_MODULES
        if (mod := importlib.import_module(f".{name}", __package__))
        and mod.detect(cwd=cwd, transcript_path=transcript_path, messages=messages)
    )
