from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cc_transcript.filterspec import event_meta
from cc_transcript.ids import SessionId
from lazy_object_proxy import Proxy

from captain_hook.session import SessionSlot, ensure_session
from captain_hook.state import RegisteredTranscript, RegisteredTranscripts
from captain_hook.util.paths import resolve_project_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cc_transcript.models import TranscriptEvent
    from cc_transcript.query import Session

# A session id becomes a filesystem path component via ``ensure_session``; external callers (CLI, MCP)
# must not smuggle path separators or traversal past that trust boundary.
INVALID_SESSION_ID = re.compile(r"[/\\]|\x00|^\.\.?$")

MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024


def lift_session(events: Sequence[TranscriptEvent], *, path: Path | None = None) -> Session:
    """Lift parsed transcript events into a query ``Session``, injecting the detected user classifier.

    The activity lift parses every tool call with ``on_error='other'``, so a
    Claude Code tool-shape change degrades to ``OtherCall`` — with a
    still-correct digest — rather than crashing every hook fire.
    """
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
        SessionActivity.from_events(session_id, list(events), user_classifier=classifier),
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
    path: str | Path | None,
    *,
    loader: Callable[[str | Path | None], Session] | None = None,
    attach: Callable[[], Sequence[Path]] | None = None,
) -> Session:
    """A ``Session`` proxy that defers parsing until an attribute is first touched.

    Events whose hooks never read the transcript never pay the parse. ``loader`` overrides the
    default :func:`load_transcript` — the resident daemon plugs in a cache-backed parse. ``attach``
    resolves external transcripts (e.g. registered codex rollouts) to fold into the session's deep
    view; it runs once, after the loader returns, on the one codepath the cold CLI and the daemon share.
    """
    resolve = loader or load_transcript

    def load() -> Session:
        try:
            session = resolve(path)
        except Exception as e:
            raise TranscriptLoadError(path) from e
        if attach and (extra := tuple(attach())):
            return dataclasses.replace(session, attachments=(*session.attachments, *extra))
        return session

    return cast("Session", Proxy(load))


def register_transcript(
    session_id: str,
    *,
    provider: str = "codex",
    thread_id: str | None = None,
    path: str | None = None,
    label: str | None = None,
) -> RegisteredTranscript:
    """Register an external transcript against ``session_id`` so it folds into the deep view.

    Exactly one non-empty ``thread_id`` (resolved lazily against the codex sessions tree at dispatch)
    or ``path`` (a direct file path, normalized to an absolute path against the registration cwd so a
    relative locator still resolves once dispatch runs from the project root) locates the transcript.
    Registration is idempotent by ``(provider, thread_id, path)`` — re-registering the same transcript
    is a no-op. This is the one write codepath; the CLI and the ``capt-hook mcp`` server both delegate here.

    Args:
        session_id: The Claude Code session the transcript attaches to.
        provider: The transcript's source provider (default ``"codex"``).
        thread_id: The provider thread/session id to resolve at dispatch.
        path: A path to the transcript file, stored absolute (a relative path anchors to the caller's cwd).
        label: An optional human label for the registration.

    Returns:
        The :class:`~captain_hook.state.RegisteredTranscript` recorded for the request.
    """
    if not session_id or INVALID_SESSION_ID.search(session_id):
        raise ValueError(f"invalid session id {session_id!r}: must not contain path separators or traversal components")
    entry = RegisteredTranscript(
        provider=provider,
        thread_id=thread_id,
        path=str(Path(path).absolute()) if path else path,
        label=label,
    )
    key = (entry.provider, entry.thread_id, entry.path)
    with SessionSlot(ensure_session(SessionId(session_id)), RegisteredTranscripts).mutate() as blob:
        if key not in {(e.provider, e.thread_id, e.path) for e in blob.entries}:
            blob.entries.append(entry)
    return entry


def readable_transcript(path: Path) -> bool:
    # A special file (FIFO/device) hangs and an oversized blob OOMs the deep view's whole-file parse.
    return path.is_file() and path.stat().st_size <= MAX_TRANSCRIPT_BYTES


def registered_paths(session_dir: Path | None) -> tuple[Path, ...]:
    """The on-disk paths of every transcript registered against ``session_dir``, unsafe entries skipped.

    A path entry resolves to its stored absolute path; a thread-id entry resolves lazily via
    :func:`cc_transcript.codex.find_transcript`. A pruned or unresolvable id, and any locator that no
    longer points at a bounded regular file (a special file or oversized blob would hang or OOM the
    deep view's whole-file parse), drops out silently.
    """
    from cc_transcript.codex import find_transcript

    return tuple(
        resolved
        for entry in SessionSlot(session_dir, RegisteredTranscripts).get(RegisteredTranscripts()).entries
        if (resolved := Path(entry.path) if entry.path else find_transcript(SessionId(entry.thread_id))) is not None
        and readable_transcript(resolved)
    )
