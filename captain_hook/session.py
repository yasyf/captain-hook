from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar, Generic, TypeVar, overload

from cc_transcript.ids import SessionId
from filelock import FileLock
from loguru import logger
from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)

STALE_AGE_SECONDS = 30 * 24 * 60 * 60


def state_root() -> Path:
    from captain_hook.util.paths import resolve_state_dir

    return resolve_state_dir()


def ensure_session(session_id: SessionId) -> Path:
    sd = state_root() / "hooks" / "sessions" / session_id
    sd.mkdir(parents=True, exist_ok=True)
    return sd


def cleanup_stale() -> None:
    from cc_transcript.discovery import find_transcript_sync

    sessions = state_root() / "hooks" / "sessions"
    if not sessions.exists():
        return
    cutoff = time.time() - STALE_AGE_SECONDS
    for sd in sessions.iterdir():
        if sd.is_dir() and sd.stat().st_mtime < cutoff and find_transcript_sync(SessionId(sd.name)) is None:
            shutil.rmtree(sd, ignore_errors=True)


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
        if not self._path or not self._path.exists():
            return default
        try:
            return self._model.model_validate_json(self._path.read_text())
        except Exception:
            logger.bind(model=self._model.__name__, path=str(self._path)).opt(exception=True).warning(
                "failed to read session state",
            )
            return default

    def set(self, obj: M) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=self._path.parent,
                suffix=".tmp",
            )
            try:
                os.write(tmp_fd, obj.model_dump_json().encode())
                os.close(tmp_fd)
                os.replace(tmp_name, self._path)
            except BaseException:
                os.close(tmp_fd) if not os.get_inheritable(tmp_fd) else None
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError:
            logger.bind(path=str(self._path)).opt(exception=True).warning("failed to persist session state")

    def delete(self) -> None:
        if self._path:
            self._path.unlink(missing_ok=True)

    @contextmanager
    def mutate(self) -> Iterator[M]:
        """Yield the loaded model under an exclusive file lock; persist it on clean exit.

        The lock is held for the whole ``with`` block, so concurrent writers — separate
        ``capt-hook run`` processes racing one session's state, or threads within a process —
        serialize rather than clobber. The body must be short: no slow work under the lock.
        A null slot (no session directory) yields an in-memory model and persists nothing.
        """
        if self._path is None:
            yield self.get(self._model())
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._path.with_name(self._path.name + ".lock")
        with FileLock(str(lock)):
            obj = self.get(self._model())
            yield obj
            self.set(obj)


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
