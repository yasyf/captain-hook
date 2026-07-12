from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cc_transcript.filterspec import event_meta
from cc_transcript.ids import SessionId
from cc_transcript.models import AssistantEvent, ToolUseBlock
from cc_transcript.tools import parse_tool_call
from lazy_object_proxy import Proxy

from captain_hook.util.paths import resolve_project_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cc_transcript.models import TranscriptEvent
    from cc_transcript.query import Session
    from cc_transcript.tools import ToolCall


class LenientToolUseBlock(ToolUseBlock):
    """A ``ToolUseBlock`` whose typed parse degrades to ``OtherCall`` instead of raising.

    The hook runtime lifts every transcript on every event, so a Claude Code
    tool-shape change must degrade — with a still-correct digest — rather
    than crash every hook fire.
    """

    @property
    def call(self) -> ToolCall:
        return parse_tool_call(self.name, self.input, on_error="other")


def lenient_event(event: TranscriptEvent) -> TranscriptEvent:
    match event:
        case AssistantEvent(blocks=blocks) if any(isinstance(block, ToolUseBlock) for block in blocks):
            return replace(
                event,
                blocks=tuple(
                    LenientToolUseBlock(id=block.id, name=block.name, input=block.input)
                    if isinstance(block, ToolUseBlock)
                    else block
                    for block in blocks
                ),
            )
        case _:
            return event


def lift_session(events: Sequence[TranscriptEvent], *, path: Path | None = None) -> Session:
    """Lift parsed transcript events into a query ``Session``, injecting the detected user classifier."""
    from cc_transcript.activity import SessionActivity
    from cc_transcript.query import Session

    from captain_hook.app import _state
    from captain_hook.classifiers import detect

    classifier = _state.classifier or detect(
        cwd=resolve_project_dir(),
        transcript_path=str(path) if path else None,
        events=events,
    )
    session_id = next(
        (meta.session_id for event in events if (meta := event_meta(event)) is not None),
        SessionId(path.stem if path else "unknown"),
    )
    return Session.from_activity(
        SessionActivity.from_events(session_id, [lenient_event(e) for e in events], user_classifier=classifier),
        path=path,
    )


def load_transcript(path: str | Path | None) -> Session:
    """Parse and lift the transcript at ``path``; a missing path yields an empty ``Session``."""
    from cc_transcript.parser import parse_events_from_bytes
    from cc_transcript.query import Session

    if not path or not (path := Path(path)).exists():
        return Session(())
    return lift_session(parse_events_from_bytes(path.read_bytes()), path=path)


class TranscriptLoadError(Exception):
    """Raised when the lazy transcript proxy fails to parse or read a transcript.

    The event path defers loading behind a proxy, so a corrupt or unreadable transcript
    first surfaces when a handler touches ``evt.ctx.transcript``. Dispatch's handler-error
    boundary re-raises this rather than swallowing it, so the process fails loudly — exactly
    as the eager baseline load did before dispatch.
    """


def lazy_transcript(
    path: str | Path | None, *, loader: Callable[[str | Path | None], Session] | None = None
) -> Session:
    """A ``Session`` proxy that defers parsing until an attribute is first touched.

    Events whose hooks never read the transcript never pay the parse. ``loader`` overrides the
    default :func:`load_transcript` — the resident daemon plugs in a cache-backed parse.
    """
    resolve = loader or load_transcript

    def load() -> Session:
        try:
            return resolve(path)
        except Exception as e:
            raise TranscriptLoadError(path) from e

    return cast("Session", Proxy(load))
