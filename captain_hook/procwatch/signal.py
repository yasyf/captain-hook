from __future__ import annotations

import os
import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from captain_hook.procwatch.identity import verify_cwd, verify_row
from captain_hook.util import proc, reqenv
from captain_hook.util.proc import Unreadable

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from captain_hook.procwatch.identity import ProcessIdentity
    from captain_hook.util.proc import ProcessRow


@dataclass(frozen=True, slots=True)
class Outcome:
    signaled: bool
    reason: str


def logged_kill(path: str) -> Callable[[int, int], None]:
    def kill(pid: int, sig: int) -> None:
        with open(path, "a", encoding="utf-8") as log:
            log.write(f"{pid} {sig}\n")

    return kill


@contextmanager
def sender(pid: int, kill: Callable[[int, int], None]) -> Generator[Callable[[int], None]]:
    if (log := reqenv.getenv("CAPT_HOOK_TEST_SIGNAL_LOG")) is not None:
        yield partial(logged_kill(log), pid)
        return
    if sys.platform != "linux":
        yield partial(kill, pid)
        return
    fd = os.pidfd_open(pid)
    try:
        yield partial(signal.pidfd_send_signal, fd)
    finally:
        os.close(fd)


def deadline_passed() -> Unreadable | None:
    reqenv.checkpoint()
    if (left := reqenv.seconds_left()) is not None and left <= 0:
        return Unreadable("the dispatch deadline passed before the signal")
    return None


def refusal(
    identity: ProcessIdentity,
    *,
    recheck: Callable[[], Unreadable | None],
    usage_row: Callable[[int], ProcessRow | None],
    process_cwd: Callable[[int], str | None],
) -> Unreadable | None:
    if (why := verify_cwd(identity, process_cwd=process_cwd) or recheck()) is not None:
        return why
    if isinstance(checked := verify_row(identity, usage_row=usage_row), Unreadable):
        return checked
    return deadline_passed()


def terminate(
    identity: ProcessIdentity,
    sig: int,
    *,
    recheck: Callable[[], Unreadable | None],
    kill: Callable[[int, int], None] = os.kill,
    usage_row: Callable[[int], ProcessRow | None] = proc.usage_row,
    process_cwd: Callable[[int], str | None] = proc.process_cwd,
) -> Outcome:
    if sig not in {signal.SIGTERM, signal.SIGKILL} or identity.pid <= 1:
        raise ValueError(f"refusing signal {sig} to pid {identity.pid}")
    if reqenv.getenv("CAPT_HOOK_TEST_NO_LIVE") == "1":
        return Outcome(False, "live signals are disabled under test")
    try:
        with sender(identity.pid, kill) as send:
            if (why := refusal(identity, recheck=recheck, usage_row=usage_row, process_cwd=process_cwd)) is not None:
                return Outcome(False, why.reason)
            send(sig)
    except ProcessLookupError:
        return Outcome(False, "exited before the signal")
    except PermissionError:
        return Outcome(False, "signal refused: permission")
    return Outcome(True, f"{signal.Signals(sig).name} sent to pid {identity.pid}.")
