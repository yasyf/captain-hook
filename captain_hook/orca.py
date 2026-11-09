"""An Orca worker's Run coordinator, read through Orca's own orchestration record."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from captain_hook.util import reqenv

DISPATCH_TTL = 60.0
PANE_STATUS = Path("agent-hooks", "last-status.json")


class OrcaTimedOut(Exception):
    """Orca's CLI sat out its whole probe timeout, which says nothing about the Run."""


@dataclass(frozen=True, slots=True)
class Binding:
    run: str
    coordinator: str
    transcript: Path | None


@dataclass(slots=True)
class Reading:
    binding: Binding | None
    read_at: float
    refreshing: bool = False


READINGS: dict[tuple[str, Path], Reading] = {}
READINGS_GUARD = threading.Lock()


def coordinator_transcript() -> Path | None:
    handle, data = reqenv.getenv("ORCA_TERMINAL_HANDLE"), reqenv.getenv("ORCA_USER_DATA_PATH")
    attended = reqenv.getenv("CLAUDE_CODE_SESSION_ATTENDED") == "1"
    binding = dispatched_run(handle, Path(data)) if attended and handle and data else None
    path = None if binding is None else binding.transcript
    return path if path is not None and path.is_file() else None


def dispatched_run(handle: str, user_data: Path) -> Binding | None:
    """The Run *handle*'s live dispatch belongs to, waiting on Orca only for a terminal's first reading.

    Every event of an Orca lane reads this before its hooks start, and so does every ticket a hook
    decides in an Orca terminal, while Orca's server can sit out a probe's whole timeout when busy,
    so a later reading answers from the last one and refreshes on a thread of its own.
    """
    key = (handle, user_data)
    with READINGS_GUARD:
        reading = READINGS.get(key)
        due = reading is not None and not reading.refreshing and time.monotonic() - reading.read_at >= DISPATCH_TTL
        if reading is not None:
            reading.refreshing |= due
    if reading is None:
        binding = read_binding(handle, user_data, unanswered=None)
        remember(key, binding)
        return binding
    if due:
        threading.Thread(
            target=refresh, args=(key, reading.binding), name="capt-hook-orca-refresh", daemon=True
        ).start()
    return reading.binding


def refresh(key: tuple[str, Path], last: Binding | None) -> None:
    binding = last
    try:
        with reqenv.use_request(reqenv.RequestOverrides({"ORCA_USER_DATA_PATH": str(key[1])}, str(key[1]), 0, "")):
            binding = read_binding(*key, unanswered=last)
    finally:
        remember(key, binding)


def remember(key: tuple[str, Path], binding: Binding | None) -> None:
    with READINGS_GUARD:
        READINGS[key] = Reading(binding, time.monotonic())


def read_binding(handle: str, user_data: Path, *, unanswered: Binding | None) -> Binding | None:
    try:
        if (found := live_run(handle)) is None:
            return None
        run, coordinator = found
        pane = terminal_pane(coordinator)
    except OrcaTimedOut:
        return unanswered
    session = None if pane is None else claude_session(pane_entries(user_data).get(pane))
    return Binding(run, coordinator, None if session is None else session[1])


def orca_answer(argv: tuple[str, ...], *path: str) -> Any:
    from captain_hook.builtin_packs.general.hooks._sessions import TimedOut, orca_json

    if isinstance(answer := orca_json(argv, *path), TimedOut):
        raise OrcaTimedOut(answer.reason)
    return answer


def live_run(handle: str) -> tuple[str, str] | None:
    """The Run *handle*'s live dispatch belongs to, and the terminal of that Run's coordinator."""
    checked = ("orca", "orchestration", "check", "--terminal", handle, "--peek", "--json")
    if not isinstance(dispatch := orca_answer(checked, "result", "dispatchId"), str):
        return None
    match orca_answer(("orca", "orchestration", "worker-show", "--dispatch", dispatch, "--json"), "result", "dispatch"):
        case {"id": found, "assigneeHandle": assignee, "status": "dispatched", "runId": str() as run} if (
            found == dispatch and assignee == handle
        ):
            shown = ("orca", "orchestration", "run-show", "--id", run, "--json")
            coordinator = orca_answer(shown, "result", "run", "coordinator_handle")
            return (run, coordinator) if isinstance(coordinator, str) else None
    return None


def terminal_pane(handle: str) -> str | None:
    match orca_answer(("orca", "terminal", "show", "--terminal", handle, "--json"), "result", "terminal"):
        case {"tabId": str() as tab, "leafId": str() as leaf}:
            return f"{tab}:{leaf}"
    return None


def pane_entries(user_data: Path) -> dict[str, object]:
    try:
        status = json.loads((user_data / PANE_STATUS).read_text())
    except (OSError, ValueError):
        return {}
    match status:
        case {"entries": dict() as entries}:
            return entries
    return {}


def claude_session(entry: object) -> tuple[str, Path] | None:
    match entry:
        case {"source": "claude", "providerSession": {"id": str() as session, "transcriptPath": str() as path}}:
            return session, Path(path)
    return None
