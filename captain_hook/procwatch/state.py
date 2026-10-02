from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

from filelock import FileLock, Timeout
from pydantic import BaseModel, Field, ValidationError

from captain_hook.desktop import client
from captain_hook.procwatch.screen import redact
from captain_hook.session import session_state
from captain_hook.util import reqenv
from captain_hook.util.fs import atomic_write
from captain_hook.util.proc import Unreadable

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from captain_hook.events import BaseHookEvent

LOCK_TIMEOUT_SECONDS = 5.0
MAX_PENDING = 20
MAX_DELIVERED = 50
MAX_SIGNALS = 50


class StateUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Notice(BaseModel):
    id: str
    text: str


class SignalRecord(BaseModel):
    identity: str
    signal: int
    at: float


@session_state
class ProcwatchState(BaseModel):
    pending: list[Notice] = Field(default_factory=list)
    delivered: list[str] = Field(default_factory=list)
    verdicts: dict[str, bool] = Field(default_factory=dict)
    judging: list[str] = Field(default_factory=list)
    judge_calls: int = 0
    signals: list[SignalRecord] = Field(default_factory=list)

    def notify(self, text: str) -> None:
        self.pending = [*self.pending, Notice(id=uuid4().hex, text=redact(text))][-MAX_PENDING:]

    def forget(self, key: str) -> None:
        self.verdicts.pop(key, None)
        self.judging = [claimed for claimed in self.judging if claimed != key]


@contextmanager
def transaction(evt: BaseHookEvent) -> Iterator[ProcwatchState]:
    if (path := evt.ctx.s[ProcwatchState].path) is None:
        raise StateUnavailable("this session has no state directory to record the resource monitor's decisions.")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(path.with_name(path.name + ".lock")), timeout=LOCK_TIMEOUT_SECONDS):
            state = load(path) if path.exists() else ProcwatchState()
            yield state
            atomic_write(path, state.model_dump_json())
    except Timeout as exc:
        raise StateUnavailable("the resource monitor's session state is locked by another writer.") from exc
    except OSError as exc:
        raise StateUnavailable("the resource monitor's session state could not be written.") from exc


def load(path: Path) -> ProcwatchState:
    try:
        return ProcwatchState.model_validate_json(path.read_text())
    except ValidationError as exc:
        raise StateUnavailable("the resource monitor's session state is corrupt.") from exc


def record_notice(evt: BaseHookEvent, text: str) -> None:
    with transaction(evt) as state:
        state.notify(text)


def record_refusal(evt: BaseHookEvent, key: str, text: str) -> None:
    with transaction(evt) as state:
        state.notify(text)
        state.forget(key)


def record_signal(evt: BaseHookEvent, key: str, signal: int, text: str) -> None:
    with transaction(evt) as state:
        state.notify(text)
        state.forget(key)
        state.signals = [*state.signals, SignalRecord(identity=key, signal=signal, at=time.time())][-MAX_SIGNALS:]


def announce(title: str, body: str) -> None:
    if reqenv.getenv("CAPT_HOOK_TEST_NO_LIVE") == "1":
        return
    client.notify(kind="performance", title=title, body=redact(body))


def pending_notice(evt: BaseHookEvent) -> bool:
    if (path := evt.ctx.s[ProcwatchState].path) is None or not path.exists():
        return False
    state = evt.ctx.s[ProcwatchState].get(ProcwatchState())
    return any(notice.id not in state.delivered for notice in state.pending)


def take_pending(evt: BaseHookEvent) -> list[Notice]:
    with transaction(evt) as state:
        fresh = [notice for notice in state.pending if notice.id not in state.delivered]
        state.delivered = [*state.delivered, *(notice.id for notice in fresh)][-MAX_DELIVERED:]
        state.pending = []
    return fresh


def claim_judgement(evt: BaseHookEvent, key: str, *, cap: int) -> bool | Unreadable | None:
    with transaction(evt) as state:
        if key in state.verdicts:
            return state.verdicts[key]
        if key in state.judging:
            return Unreadable("its disposability verdict is already in flight.")
        if state.judge_calls >= cap:
            return Unreadable("this session spent its judge budget.")
        state.judge_calls += 1
        state.judging = [*state.judging, key]
        return None


def release_judgement(evt: BaseHookEvent, key: str) -> None:
    with transaction(evt) as state:
        state.judging = [claimed for claimed in state.judging if claimed != key]
        state.judge_calls -= 1


def settle_judgement(evt: BaseHookEvent, key: str, verdict: bool) -> None:
    with transaction(evt) as state:
        state.judging = [claimed for claimed in state.judging if claimed != key]
        state.verdicts[key] = verdict


def signaled(evt: BaseHookEvent, key: str, signal: int) -> bool:
    with transaction(evt) as state:
        return any(record.identity == key and record.signal == signal for record in state.signals)
