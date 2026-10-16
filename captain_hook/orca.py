"""An Orca worker's Run coordinator, read through Orca's own orchestration record."""

from __future__ import annotations

import json
from pathlib import Path

from captain_hook.util import reqenv
from captain_hook.util.caching import ttl_cache

DISPATCH_TTL = 60.0
PANE_STATUS = Path("agent-hooks", "last-status.json")


def coordinator_transcript() -> Path | None:
    handle, data = reqenv.getenv("ORCA_TERMINAL_HANDLE"), reqenv.getenv("ORCA_USER_DATA_PATH")
    attended = reqenv.getenv("CLAUDE_CODE_SESSION_ATTENDED") == "1"
    path = run_coordinator(handle, Path(data)) if attended and handle and data else None
    return path if path is not None and path.is_file() else None


@ttl_cache(DISPATCH_TTL)
def run_coordinator(handle: str, user_data: Path) -> Path | None:
    if (coordinator := coordinator_handle(handle)) is None or (pane := terminal_pane(coordinator)) is None:
        return None
    return None if (found := claude_session(pane_entries(user_data).get(pane))) is None else found[1]


def coordinator_handle(handle: str) -> str | None:
    """The terminal of the Run coordinator that *handle*'s live dispatch answers to."""
    from captain_hook.builtin_packs.general.hooks._sessions import orca_json

    checked = ("orca", "orchestration", "check", "--terminal", handle, "--peek", "--json")
    if not isinstance(dispatch := orca_json(checked, "result", "dispatchId"), str):
        return None
    match orca_json(("orca", "orchestration", "worker-show", "--dispatch", dispatch, "--json"), "result", "dispatch"):
        case {"id": found, "assigneeHandle": assignee, "status": "dispatched", "runId": str() as run} if (
            found == dispatch and assignee == handle
        ):
            shown = ("orca", "orchestration", "run-show", "--id", run, "--json")
            coordinator = orca_json(shown, "result", "run", "coordinator_handle")
            return coordinator if isinstance(coordinator, str) else None
    return None


def terminal_pane(handle: str) -> str | None:
    from captain_hook.builtin_packs.general.hooks._sessions import orca_json

    match orca_json(("orca", "terminal", "show", "--terminal", handle, "--json"), "result", "terminal"):
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
