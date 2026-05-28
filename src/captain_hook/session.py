from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


def state_root() -> Path:
    from captain_hook.settings import resolve_state_dir

    return resolve_state_dir()


def session_hash(transcript_path: str | Path) -> str:
    return sha256(str(transcript_path).encode()).hexdigest()[:12]


def ensure_session(transcript_path: str | Path) -> Path:
    sd = state_root() / "hooks" / "sessions" / session_hash(transcript_path)
    sd.mkdir(parents=True, exist_ok=True)
    marker = sd / ".transcript_path"
    if not marker.exists():
        marker.write_text(str(transcript_path))
    return sd


def cleanup_stale() -> None:
    sessions = state_root() / "hooks" / "sessions"
    if not sessions.exists():
        return
    for sd in sessions.iterdir():
        if not sd.is_dir():
            continue
        marker = sd / ".transcript_path"
        if marker.exists() and not Path(marker.read_text().strip()).exists():
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

    def get(self, default: M | None = None) -> M | None:
        if not self._path or not self._path.exists():
            return default
        try:
            return self._model.model_validate_json(self._path.read_text())
        except Exception:
            logger.warning("Failed to read %s from %s", self._model.__name__, self._path, exc_info=True)
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
            logger.warning("Failed to persist %s", self._path, exc_info=True)

    def delete(self) -> None:
        if self._path:
            self._path.unlink(missing_ok=True)


class SessionStore:
    """Class-keyed store providing typed ``SessionSlot`` access via ``store[ModelClass]``."""

    TRACKED: ClassVar[list[type[BaseModel]]] = []

    def __init__(self, session_dir: Path | None) -> None:
        self._dir = session_dir

    def __getitem__(self, model: type[M]) -> SessionSlot[M]:
        return SessionSlot(self._dir, model)

    @classmethod
    def track(cls, model: type[BaseModel]) -> None:
        """Register ``model`` so it appears in ``tracked_models()`` and ``tracked_paths()``."""
        if model not in cls.TRACKED:
            cls.TRACKED.append(model)

    @classmethod
    def untrack(cls, model: type[BaseModel]) -> None:
        """Reverse ``track`` — primarily for test isolation."""
        if model in cls.TRACKED:
            cls.TRACKED.remove(model)

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
