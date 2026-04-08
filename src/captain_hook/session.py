from __future__ import annotations

import os
import re
import shutil
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)


def state_root() -> Path:
    """Return the root directory for hook state storage.

    Returns:
        Path from ``CLAUDE_HOOKS_STATE_DIR`` env var, or ``~/.claude/state``.
    """
    return Path(os.environ.get("CLAUDE_HOOKS_STATE_DIR", Path.home() / ".claude" / "state"))


def session_hash(transcript_path: str | Path) -> str:
    """Compute a 12-char hex hash of a transcript path for session directory naming.

    Args:
        transcript_path: Path to the transcript file.

    Returns:
        First 12 characters of the SHA-256 hex digest.
    """
    return sha256(str(transcript_path).encode()).hexdigest()[:12]


def ensure_session(transcript_path: str | Path) -> Path:
    """Create and return the session directory for a transcript, creating it if needed.

    Args:
        transcript_path: Path to the transcript file.

    Returns:
        Path to the session directory.
    """
    sd = state_root() / "hooks" / "sessions" / session_hash(transcript_path)
    sd.mkdir(parents=True, exist_ok=True)
    marker = sd / ".transcript_path"
    if not marker.exists():
        marker.write_text(str(transcript_path))
    return sd


def cleanup_stale() -> None:
    """Remove session directories whose transcript files no longer exist."""
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
    """A typed slot for reading/writing a single Pydantic model in a session directory.

    Provides atomic file writes via ``set()`` and lazy deserialization via ``get()``.
    The filename is derived from the model class name (e.g. ``HookState`` → ``hook_state.json``).

    Args:
        session_dir: Directory for session state files, or None to disable persistence.
        model: Pydantic model class for serialization.
    """

    def __init__(self, session_dir: Path | None, model: type[M]) -> None:
        self._model = model
        self._path = (session_dir / f"{self.model_key(model)}.json") if session_dir else None

    @staticmethod
    def model_key(model: type[BaseModel]) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", model.__name__).lower()

    @property
    def path(self) -> Path | None:
        return self._path

    def get(self) -> M | None:
        if not self._path or not self._path.exists():
            return None
        try:
            return self._model.model_validate_json(self._path.read_text())
        except Exception:
            return None

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
            pass

    def delete(self) -> None:
        if self._path:
            self._path.unlink(missing_ok=True)


class SessionStore:
    """Class-keyed store providing typed ``SessionSlot`` access via ``store[ModelClass]``.

    Args:
        session_dir: Root directory for session state files.
    """

    def __init__(self, session_dir: Path | None) -> None:
        self._dir = session_dir

    def __getitem__(self, model: type[M]) -> SessionSlot[M]:
        return SessionSlot(self._dir, model)
