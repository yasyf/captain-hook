"""Durable, cross-session state: the durable twin of ``SessionStore``.

Where :class:`~captain_hook.SessionStore` is scoped to one session, a
:class:`DurableStore` persists across sessions, keyed by project (default) or globally.
Its slots add a ``filelock``-guarded :meth:`DurableSlot.mutate` so concurrent writers
across sessions never lose an update.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from hashlib import sha256
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self

from filelock import FileLock
from loguru import logger
from pydantic import BaseModel

from captain_hook.session import SessionSlot

if TYPE_CHECKING:
    from pathlib import Path

    from captain_hook.events import BaseHookEvent

Scope = Literal["project", "global"]


def durable_root() -> Path:
    from captain_hook.settings import resolve_state_dir

    return resolve_state_dir() / "hooks" / "durable"


def project_key(repo_root: Path) -> str:
    return f"{repo_root.name}-{sha256(str(repo_root.resolve()).encode()).hexdigest()[:16]}"


def durable_dir(scope: Scope, repo_root: Path | None) -> Path | None:
    match scope:
        case "global":
            return durable_root() / "global"
        case "project" if repo_root is not None:
            return durable_root() / "projects" / project_key(repo_root)
        case "project":
            logger.warning("durable project state has no repo_root; not persisting")
            return None
        case _:
            raise ValueError(f"scope must be 'project' or 'global', got {scope!r}")


class DurableSlot[M: BaseModel](SessionSlot[M]):
    """A :class:`SessionSlot` rooted in a durable directory, with a locked :meth:`mutate`."""

    @contextmanager
    def mutate(self) -> Iterator[M]:
        """Yield the loaded model under an exclusive file lock; persist it on clean exit.

        The lock is held for the whole ``with`` block, so concurrent writers across sessions
        serialize rather than clobber. The body must be short — no slow work under the lock.
        A null slot (project scope with no ``repo_root``) yields an in-memory model and
        persists nothing.
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


class DurableStore:
    """Class-keyed durable store providing typed :class:`DurableSlot` access via ``store[Model]``."""

    TRACKED: ClassVar[list[type[BaseModel]]] = []

    def __init__(self, directory: Path | None) -> None:
        self._dir = directory

    def __getitem__[M: BaseModel](self, model: type[M]) -> DurableSlot[M]:
        return DurableSlot(self._dir, model)

    def load[M: BaseModel](self, model: type[M]) -> M:
        return self[model].get(model())

    @classmethod
    def for_event(cls, evt: BaseHookEvent, *, scope: Scope = "project") -> DurableStore:
        return cls(durable_dir(scope, evt.ctx.repo_root))

    @classmethod
    def track(cls, model: type[BaseModel]) -> None:
        if model not in cls.TRACKED:
            cls.TRACKED.append(model)

    @classmethod
    def untrack(cls, model: type[BaseModel]) -> None:
        if model in cls.TRACKED:
            cls.TRACKED.remove(model)

    @classmethod
    def tracked_models(cls) -> Sequence[type[BaseModel]]:
        return tuple(cls.TRACKED)


class DurableState(BaseModel):
    """Base for a model persisted across sessions, scoped by the ``scope`` class keyword.

    Declare the scope at subclass time — ``class Foo(DurableState, scope="global")``; it
    defaults to ``"project"`` (keyed by the repo root). The subclass carries ``load``/``save``/
    ``reset`` plus a locked :meth:`mutate` context manager. Reach for it when state must
    outlive a single session; for within-session sharing use
    [`workflow_state`][captain_hook.workflow_state] instead.

    Example:
        >>> from captain_hook import Deque
        >>> class JsonShapes(DurableState, scope="global"):
        ...     shapes: Deque[256]
    """

    __durable_scope__: ClassVar[Scope] = "project"

    def __init_subclass__(cls, *, scope: Scope = "project", **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if scope not in ("project", "global"):
            raise ValueError(f"scope must be 'project' or 'global', got {scope!r}")
        cls.__durable_scope__ = scope
        DurableStore.track(cls)

    @classmethod
    def load(cls, evt: BaseHookEvent) -> Self:
        return DurableStore.for_event(evt, scope=cls.__durable_scope__).load(cls)

    def save(self, evt: BaseHookEvent) -> None:
        DurableStore.for_event(evt, scope=type(self).__durable_scope__)[type(self)].set(self)

    @classmethod
    def reset(cls, evt: BaseHookEvent) -> None:
        DurableStore.for_event(evt, scope=cls.__durable_scope__)[cls].delete()

    @classmethod
    @contextmanager
    def mutate(cls, evt: BaseHookEvent) -> Iterator[Self]:
        with DurableStore.for_event(evt, scope=cls.__durable_scope__)[cls].mutate() as obj:
            yield obj
