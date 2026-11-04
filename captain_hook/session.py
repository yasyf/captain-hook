from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar, overload

from cc_transcript.ids import SessionId
from filelock import FileLock, Timeout
from loguru import logger
from pydantic import BaseModel

from captain_hook.dispatch_metrics import measured_call, note_write
from captain_hook.util.fs import atomic_write

M = TypeVar("M", bound=BaseModel)

STALE_AGE_SECONDS = 30 * 24 * 60 * 60
COMMIT_LOCK_SECONDS = 2.0
ABSENT = object()


def state_root() -> Path:
    from captain_hook.util.paths import resolve_state_dir

    return resolve_state_dir()


def ensure_session(session_id: SessionId) -> Path:
    sd = state_root() / "hooks" / "sessions" / session_id
    sd.mkdir(parents=True, exist_ok=True)
    return sd


def cleanup_stale(exclude: SessionId | None = None) -> None:
    """Reap session dirs untouched for STALE_AGE_SECONDS whose transcript is gone.

    A stale dir whose transcript still exists is touched, so each one costs one
    transcript search per STALE_AGE_SECONDS rather than one on every SessionStart.

    ``exclude`` skips the current session unconditionally — the caller's own dir is live
    even if a clock skew or a transcript-discovery miss would otherwise mark it stale.
    """
    from cc_transcript.codex import sessions_root
    from cc_transcript.discovery import CLAUDE_PROJECTS_DIR

    from captain_hook.snapshots.client import client_scope
    from captain_hook.transcripts import resolved_transcript_paths

    sessions = state_root() / "hooks" / "sessions"
    if not sessions.exists():
        return
    cutoff = time.time() - STALE_AGE_SECONDS
    stale = [(sd, sd.stat()) for sd in sessions.iterdir() if sd.name != exclude and sd.is_dir()]
    stale = [(sd, initial) for sd, initial in stale if initial.st_mtime < cutoff]
    if not stale:
        return
    with client_scope() as client:
        resolved = resolved_transcript_paths(
            client, [SessionId(sd.name) for sd, _ in stale], roots=[CLAUDE_PROJECTS_DIR, sessions_root()]
        )
    for sd, initial in stale:
        if sd.name == exclude:
            continue
        if resolved[SessionId(sd.name)] is not None:
            os.utime(sd)
            continue
        try:
            current = sd.stat()
        except FileNotFoundError:
            continue
        if (current.st_dev, current.st_ino) == (initial.st_dev, initial.st_ino) and current.st_mtime < cutoff:
            shutil.rmtree(sd, ignore_errors=True)


def merge_json(base: Any, ours: Any, theirs: Any) -> Any:
    """Three-way merge of two writers' JSON: dicts by key, lists by what each side added or removed, else ours."""
    if ours == base or ours == theirs:
        return theirs
    if theirs == base:
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        was = base if isinstance(base, dict) else {}
        merged = {
            key: merge_json(was.get(key, ABSENT), ours.get(key, ABSENT), theirs.get(key, ABSENT))
            for key in ours | theirs
        }
        return {key: value for key, value in merged.items() if value is not ABSENT}
    if isinstance(ours, list) and isinstance(theirs, list):
        was = base if isinstance(base, list) else []
        kept = [item for item in theirs if item in ours or item not in was]
        return kept + [item for item in ours if item not in was and item not in theirs]
    return ours


@dataclass
class Staged:
    """One slot's pending write in a :class:`StateBatch`: the file's text when last read, and the text to leave there."""

    owner: SessionSlot[Any]
    lock: threading.Lock = field(default_factory=threading.Lock)
    base: str | None = None
    text: str | None = None
    dirty: bool = False
    overwrite: bool = False


class StateBatch:
    """One dispatch's state writes, staged in memory and committed together when the dispatch ends.

    However many hooks ran, the dispatch replaces each file it changed once, and every hook of the
    dispatch reads what the others staged. A concurrent dispatch sees them at the commit, which merges
    with whatever that dispatch committed first.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._staged: dict[Path, Staged] = {}
        self._open = True

    def entry(self, slot: SessionSlot[Any], path: Path) -> Staged | None:
        with self._guard:
            return self._staged.setdefault(path, Staged(owner=slot)) if self._open else None

    def dirty(self, path: Path) -> Staged | None:
        with self._guard:
            staged = self._staged.get(path) if self._open else None
            return staged if staged is not None and staged.dirty else None

    def stage(self, staged: Staged, text: str | None, *, overwrite: bool = False) -> bool:
        with self._guard:
            if not self._open:
                return False
            staged.text, staged.dirty = text, True
            staged.overwrite |= overwrite
            return True

    def commit(self) -> None:
        with self._guard:
            self._open = False
            pending = [staged for staged in self._staged.values() if staged.dirty]
        deadline = time.monotonic() + COMMIT_LOCK_SECONDS
        for staged in pending:
            staged.owner.commit(staged, max(0.0, deadline - time.monotonic()))


STATE_BATCH: ContextVar[StateBatch | None] = ContextVar("captain_hook_state_batch", default=None)


@contextmanager
def batched_state() -> Iterator[None]:
    """Stage every slot write made in this context, hook threads included, and commit them all on exit."""
    if STATE_BATCH.get() is not None:
        yield
        return
    batch = StateBatch()
    token = STATE_BATCH.set(batch)
    try:
        yield
    finally:
        STATE_BATCH.reset(token)
        batch.commit()


class SessionSlot(Generic[M]):  # noqa: UP046
    """A typed slot for reading/writing a single Pydantic model in a session directory."""

    def __init__(self, session_dir: Path | None, model: type[M]) -> None:
        self._model = model
        self._path = (session_dir / f"{self.model_key(model)}.json") if session_dir else None

    @staticmethod
    def model_key(model: type[BaseModel]) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", model.__name__).lower()

    @property
    def path(self) -> Path | None:
        return self._path

    @overload
    def get(self) -> M | None: ...
    @overload
    def get(self, default: M) -> M: ...
    def get(self, default: M | None = None) -> M | None:
        return self._load(default)[0]

    def set(self, obj: M) -> None:
        if self._path and not self._stage_overwrite(self._path, obj.model_dump_json()):
            self._persist(self._path, obj.model_dump_json(), None)

    def delete(self) -> None:
        if self._path and not self._stage_overwrite(self._path, None):
            self._path.unlink(missing_ok=True)

    def _stage_overwrite(self, path: Path, text: str | None) -> bool:
        if (batch := STATE_BATCH.get()) is None or (staged := batch.entry(self, path)) is None:
            return False
        return batch.stage(staged, text, overwrite=True)

    @contextmanager
    def mutate(self, *, timeout: float = -1) -> Iterator[M]:
        """Yield the loaded model under an exclusive file lock; persist it on clean exit.

        The lock is held for the whole ``with`` block, so concurrent writers — separate
        ``capt-hook run`` processes racing one session's state, or threads within a process —
        serialize rather than clobber. The body must be short: no slow work under the lock.
        A null slot (no session directory) yields an in-memory model and persists nothing, and a
        model that comes back unchanged is not rewritten.
        ``timeout`` is the lock-acquire budget in seconds (``-1`` waits indefinitely); pass
        ``timeout=0`` for a non-blocking acquire that raises ``filelock.Timeout`` on contention.
        Inside :func:`batched_state` the lock serializes this dispatch's hooks alone, and the write
        waits for the batch's commit, which merges with any other process's commit in between.
        """
        if self._path is None:
            yield self.get(self._model())
            return
        if (batch := STATE_BATCH.get()) is not None and (staged := batch.entry(self, self._path)) is not None:
            if not staged.lock.acquire(timeout=timeout):
                raise Timeout(str(self._path))
            try:
                obj, stored = self._load(self._model())
                if not staged.dirty:
                    staged.base = stored
                yield obj
                text = obj.model_dump_json()
                if text == stored:
                    note_write("same")
                elif not batch.stage(staged, text):
                    self.commit(replace(staged, text=text), COMMIT_LOCK_SECONDS)
            finally:
                staged.lock.release()
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._path.with_name(self._path.name + ".lock")
        with FileLock(str(lock), timeout=timeout):
            obj, stored = self._load(self._model())
            yield obj
            self._persist(self._path, obj.model_dump_json(), stored)

    def commit(self, staged: Staged, timeout: float) -> None:
        """Leave *staged*'s text in the slot's file under its lock, merged with any write since it was read."""
        path = self._path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(path.with_name(path.name + ".lock")), timeout=timeout):
                current = path.read_text() if path.exists() else None
                if (text := self._reconciled(staged, current)) is None:
                    path.unlink(missing_ok=True)
                else:
                    self._persist(path, text, current)
        except OSError:
            logger.bind(path=str(path)).opt(exception=True).warning("failed to commit session state")

    def _reconciled(self, staged: Staged, current: str | None) -> str | None:
        if staged.overwrite or staged.text is None or current == staged.base:
            return staged.text
        try:
            merged = merge_json(
                json.loads(staged.base) if staged.base else {},
                json.loads(staged.text),
                json.loads(current) if current else {},
            )
            return self._model.model_validate(merged).model_dump_json()
        except ValueError:
            return staged.text

    def _stored(self, path: Path) -> str | None:
        if (batch := STATE_BATCH.get()) is not None and (staged := batch.dirty(path)) is not None:
            return staged.text
        return path.read_text() if path.exists() else None

    def _load[D](self, default: D) -> tuple[M | D, str | None]:
        if not self._path:
            return default, None
        try:
            if (text := self._stored(self._path)) is None:
                return default, None
            return self._model.model_validate_json(text), text
        except Exception:
            logger.bind(model=self._model.__name__, path=str(self._path)).opt(exception=True).warning(
                "failed to read session state",
            )
            return default, None

    def _persist(self, path: Path, text: str, stored: str | None) -> None:
        if text == stored:
            note_write("same")
            return
        try:
            measured_call("state_write", self._model.__name__, atomic_write, path, text)
            note_write("uncompared" if stored is None else "different")
        except OSError:
            logger.bind(path=str(path)).opt(exception=True).warning("failed to persist session state")

    @contextmanager
    def strict_mutate(self) -> Iterator[M]:
        """Yield the loaded model under the slot's file lock like :meth:`mutate`, but never fall back.

        For state that grants authority once: an absent file loads a fresh model, while a read,
        parse, or validation failure raises, and so does a failed persist. The model is written back
        only when the ``with`` block changed it. A null slot (no session directory) yields a fresh
        in-memory model and persists nothing.
        """
        if self._path is None:
            yield self._model()
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._path.with_name(self._path.name + ".lock")
        with FileLock(str(lock)):
            try:
                text = self._path.read_text()
            except FileNotFoundError:
                obj = self._model()
            else:
                obj = self._model.model_validate_json(text)
            loaded = obj.model_copy(deep=True)
            yield obj
            if obj != loaded:
                atomic_write(self._path, obj.model_dump_json())


class SessionStore:
    """Class-keyed store providing typed ``SessionSlot`` access via ``store[ModelClass]``."""

    TRACKED: ClassVar[list[type[BaseModel]]] = []

    def __init__(self, session_dir: Path | None) -> None:
        self._dir = session_dir

    def __getitem__(self, model: type[M]) -> SessionSlot[M]:
        return SessionSlot(self._dir, model)

    def load(self, model: type[M]) -> M:
        """Read ``model`` from its session slot, defaulting to a fresh ``model()``.

        Args:
            model: The Pydantic model class to read.

        Returns:
            The persisted instance, or a newly constructed ``model()`` when no
            stored state exists for this session.
        """
        return self[model].get(model())

    def once(self, key: str, *, scope: str | None = None) -> bool:
        """Return ``True`` the first time ``(scope, key)`` is seen this session, ``False`` thereafter.

        Keyed, scoped dedup for hook authors — the single-key case of :meth:`unseen`.
        """
        return bool(self.unseen([key], scope=scope))

    def unseen(self, keys: Iterable[str], *, scope: str | None = None) -> list[str]:
        """Return the first-sight ``keys`` under ``scope``, recording the whole fresh subset in one write.

        De-duplicates within the batch (order-preserving) and marks every returned key before any
        downstream filtering, so a batch is never partially recorded; no write when nothing is fresh.
        ``scope`` namespaces independent call sites on the shared session store.
        """
        from captain_hook.state import SeenKeys

        deduped = list(dict.fromkeys(keys))
        prior = self.load(SeenKeys).seen.get(scope or "", [])
        if not [key for key in deduped if key not in prior]:
            return []
        with self[SeenKeys].mutate() as blob:
            seen = blob.seen.setdefault(scope or "", [])
            if not (fresh := [key for key in deduped if key not in seen]):
                return []
            seen.extend(fresh)
            return fresh

    @classmethod
    def track(cls, model: type[BaseModel]) -> None:
        """Register ``model`` so it appears in ``tracked_models()`` and ``tracked_paths()``."""
        identity = (model.__module__, model.__qualname__)
        if identity not in {(m.__module__, m.__qualname__) for m in cls.TRACKED}:
            cls.TRACKED.append(model)

    @classmethod
    def tracked_models(cls) -> Sequence[type[BaseModel]]:
        """Return the registered tracked-state models as an immutable tuple."""
        return tuple(cls.TRACKED)

    def tracked_paths(self) -> dict[str, Path]:
        """Return ``{ModelClass.__name__: Path}`` for every tracked model whose slot has a path."""
        return {m.__name__: p for m in type(self).TRACKED if (p := self[m].path)}


def session_state[T: BaseModel](cls: type[T]) -> type[T]:
    """Decorator that registers a Pydantic model for collective ``SessionStore`` introspection.

    Example:
        >>> @session_state
        ... class Snapshot(BaseModel):
        ...     op_id: str
    """
    SessionStore.track(cls)
    return cls
