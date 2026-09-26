from __future__ import annotations

import dataclasses
import re
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, overload

from cc_transcript.filterspec import event_meta
from cc_transcript.ids import SessionId

from captain_hook.session import SessionSlot, ensure_session
from captain_hook.state import RegisteredTranscript, RegisteredTranscripts
from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_project_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cc_transcript.activity import UserClassifier
    from cc_transcript.models import TranscriptEvent
    from cc_transcript.query import Session

    from captain_hook.snapshots.client import RemoteSession, SnapshotClient

# A session id becomes a filesystem path component via ``ensure_session``; external callers (CLI, MCP)
# must not smuggle path separators or traversal past that trust boundary.
INVALID_SESSION_ID = re.compile(r"[/\\]|\x00|^\.\.?$")


def user_classifier(events: Sequence[TranscriptEvent], *, path: Path | None = None) -> UserClassifier:
    from captain_hook.app import _state
    from captain_hook.classifiers import detect

    return _state.classifier or detect(
        cwd=resolve_project_dir(),
        transcript_path=str(path) if path else None,
        events=events,
    )


def lift_session(events: Sequence[TranscriptEvent], *, path: Path | None = None) -> Session:
    return lift_classified(events, user_classifier(events, path=path), path=path)


def lift_classified(
    events: Sequence[TranscriptEvent], classifier: UserClassifier, *, path: Path | None = None
) -> Session:
    from cc_transcript.activity import SessionActivity
    from cc_transcript.query import Session

    return Session.from_activity(
        SessionActivity.from_events(transcript_session_id(events, path=path), list(events), user_classifier=classifier),
        path=path,
    )


def transcript_session_id(events: Sequence[TranscriptEvent], *, path: Path | None = None) -> SessionId:
    return next(
        (meta.session_id for event in events if (meta := event_meta(event)) is not None),
        SessionId(path.stem if path else "unknown"),
    )


@overload
def load_transcript(path: str | Path) -> RemoteSession: ...


@overload
def load_transcript(path: None) -> Session: ...


def load_transcript(path: str | Path | None) -> Session | RemoteSession:
    from cc_transcript.query import Session

    from captain_hook.snapshots.client import CURRENT_CLIENT, EvidenceIncomplete

    if not path:
        return Session(())
    if (client := CURRENT_CLIENT.get()) is None:
        raise EvidenceIncomplete("invalid_request", "transcript loading requires an admitted snapshot client")
    from captain_hook.app import _state

    session = client.acquire(path)
    try:
        if _state.classifier is not None:
            from captain_hook.cli import CliState
            from captain_hook.daemon.registry import Fingerprint

            project_dir = resolve_project_dir()
            policy = {
                "id": "captain-configured",
                "version": _state.registry_fingerprint
                or Fingerprint.compute(CliState(root=Path(project_dir) if project_dir else reqenv.cwd())).digest,
            }
            return client.classify(session, _state.classifier, policy)
        data = list(
            client.pages(
                "prepare_hook_view",
                domain=True,
                view=session.view(),
                cwd=resolve_project_dir(),
                droid=reqenv.getenv("FACTORY_PROJECT_DIR") is not None,
            )
        )
        if len(data) != 1 or data[0]["kind"] != "classifier":
            raise EvidenceIncomplete("invalid_request", "hook classifier preparation returned invalid evidence")
        return session.with_classifier(data[0]["classifier"])
    except BaseException:
        session.release()
        raise


def lane_transcript_path(transcript_path: str | Path, agent_id: str) -> Path:
    """The lane transcript a subagent or teammate writes beside its parent session transcript.

    Claude Code attaches ``agent_transcript_path`` only to ``SubagentStop``; every other event
    fired inside a lane carries the lane's ``agent_id`` and the *parent* session's
    ``transcript_path``, so the lane's own file resolves by convention:
    ``<parent-dir>/<parent-stem>/subagents/agent-<agent_id>.jsonl``.
    """
    return Path(transcript_path).with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"


class TranscriptLoadError(Exception):
    """Raised when the lazy transcript proxy fails to parse or read a transcript.

    The event path defers loading behind a proxy, so a corrupt or unreadable transcript
    first surfaces when a handler touches ``evt.ctx.transcript``. Dispatch's handler-error
    boundary re-raises this rather than swallowing it, so the process fails loudly — exactly
    as the eager baseline load did before dispatch.
    """


class TranscriptPins:
    def __init__(self, load: Callable[[], Session | RemoteSession]) -> None:
        self.load = load
        self.guard = threading.Lock()
        self.pending = 1
        self.source: Session | RemoteSession | None = None

    def branch(self) -> LazyTranscript:
        with self.guard:
            if self.pending == 0:
                raise TranscriptLoadError("transcript preparation group is closed")
            self.pending += 1
        return LazyTranscript(self, seed=False)

    def borrow(self, *, seed: bool) -> Session | RemoteSession:
        from captain_hook.snapshots.client import RemoteSession

        with self.guard:
            if self.source is None:
                self.source = self.load()
            return self.source.retain() if isinstance(self.source, RemoteSession) and not seed else self.source

    def settle(self) -> None:
        from captain_hook.snapshots.client import RemoteSession

        with self.guard:
            self.pending -= 1
            source = self.source if self.pending == 0 else None
        if isinstance(source, RemoteSession):
            source.release()


class LazyTranscript:
    def __init__(self, pins: TranscriptPins, *, seed: bool) -> None:
        self.pins = pins
        self.seed = seed
        self.guard = threading.Lock()
        self.session: Session | RemoteSession | None = None
        self.released = False

    def resolve(self) -> Session | RemoteSession:
        from captain_hook.snapshots.client import EvidenceIncomplete

        with self.guard:
            if self.released:
                raise EvidenceIncomplete("stale_handle", "transcript preparation branch is closed")
            if self.session is None:
                self.session = self.pins.borrow(seed=self.seed)
            return self.session

    def fork(self) -> LazyTranscript:
        from captain_hook.snapshots.client import EvidenceIncomplete

        with self.guard:
            if self.released:
                raise EvidenceIncomplete("stale_handle", "transcript preparation branch is closed")
            return self.pins.branch()

    def release(self) -> None:
        from captain_hook.snapshots.client import RemoteSession

        with self.guard:
            if self.released:
                return
            self.released = True
            session = self.session if not self.seed else None
        try:
            if isinstance(session, RemoteSession):
                session.release()
        finally:
            self.pins.settle()

    @property
    def __class__(self) -> type:
        return type(self.resolve())

    def __getattr__(self, name: str) -> object:
        return getattr(self.resolve(), name)

    def __len__(self) -> int:
        return len(self.resolve())

    def __bool__(self) -> bool:
        return bool(self.resolve())


def fork_transcript(transcript: Session | RemoteSession | LazyTranscript) -> Session | RemoteSession | LazyTranscript:
    from captain_hook.snapshots.client import RemoteSession

    if type(transcript) is LazyTranscript:
        return transcript.fork()
    return transcript.retain() if isinstance(transcript, RemoteSession) else transcript


def release_transcript(transcript: Session | RemoteSession | LazyTranscript) -> None:
    from captain_hook.snapshots.client import RemoteSession

    if type(transcript) is LazyTranscript:
        transcript.release()
    elif isinstance(transcript, RemoteSession):
        transcript.release()


def lazy_transcript(
    path: str | Path | None,
    *,
    loader: Callable[[str | Path | None], Session | RemoteSession] | None = None,
    attach: Callable[[], Sequence[Path]] | None = None,
) -> LazyTranscript:
    resolve = loader or load_transcript

    def load() -> Session | RemoteSession:
        from captain_hook.snapshots.client import EvidenceIncomplete

        reqenv.checkpoint()
        try:
            session = resolve(path)
        except EvidenceIncomplete:
            raise
        except Exception as exc:
            raise TranscriptLoadError(path) from exc
        if attach and (extra := tuple(attach())):
            session = dataclasses.replace(session, attachments=(*session.attachments, *extra))
        return session

    return LazyTranscript(TranscriptPins(load), seed=True)


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


def resolved_transcript_paths(
    client: SnapshotClient, session_ids: Sequence[SessionId], *, roots: Sequence[Path]
) -> dict[SessionId, Path | None]:
    from captain_hook.snapshots.client import NATIVE_CLASSIFIER, EvidenceIncomplete, Lease, SnapshotProtocolError

    found: dict[SessionId, Path | None] = {}
    unique = list(dict.fromkeys(session_ids))
    for offset in range(0, len(unique), 16):
        batch = unique[offset : offset + 16]
        resolved: dict[SessionId, Path | None] = {}
        for page in client.pages(
            "resolve", session_ids=batch, roots=[str(root) for root in roots], classifier=NATIVE_CLASSIFIER
        ):
            with ExitStack() as cleanup:
                items = []
                for item in page["sessions"]:
                    lease = Lease(client, item["description"]) if item["description"] is not None else None
                    if lease is not None:
                        cleanup.callback(lease.release)
                    items.append((item, lease))
                for item, lease in items:
                    try:
                        session_id = SessionId(item["session_id"])
                        if session_id not in batch or session_id in resolved:
                            raise SnapshotProtocolError("resolve returned an unexpected or duplicate session")
                        description = item["description"]
                        if item["status"] == "missing" and description is None:
                            resolved[session_id] = None
                        elif item["status"] == "ok" and description is not None:
                            resolved[session_id] = Path(description["canonical_path"])
                        elif item["status"] == "incomplete":
                            raise EvidenceIncomplete("incomplete", "transcript resolution did not complete")
                        else:
                            raise SnapshotProtocolError("resolve returned inconsistent session availability")
                    finally:
                        if lease is not None:
                            lease.release()
        if set(resolved) != set(batch):
            raise SnapshotProtocolError("resolve omitted a requested session")
        found.update(resolved)
    return found


def registered_paths(session_dir: Path | None) -> tuple[Path, ...]:
    from cc_transcript.codex import sessions_root

    from captain_hook.snapshots.client import EvidenceIncomplete, client_scope

    entries = SessionSlot(session_dir, RegisteredTranscripts).get(RegisteredTranscripts()).entries
    if not entries:
        return ()
    with client_scope() as client:
        resolved = resolved_transcript_paths(
            client,
            [SessionId(entry.thread_id) for entry in entries if entry.thread_id],
            roots=[sessions_root()],
        )
        paths: list[Path] = []
        for entry in entries:
            if entry.path:
                try:
                    session = client.acquire(entry.path)
                except EvidenceIncomplete as exc:
                    if exc.status != "missing":
                        raise
                    continue
                try:
                    paths.append(session.path)
                finally:
                    session.release()
            else:
                assert entry.thread_id is not None
                if (path := resolved[SessionId(entry.thread_id)]) is not None:
                    paths.append(path)
    return tuple(paths)
