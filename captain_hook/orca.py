"""An Orca worker's dispatching coordinator, read through Orca's own dispatch record."""

from __future__ import annotations

import json
from pathlib import Path

from captain_hook.util import reqenv
from captain_hook.util.caching import ttl_cache

DISPATCH_TTL = 60.0
PANE_STATUS = Path("agent-hooks", "last-status.json")


def coordinator_transcript(session_id: str | None) -> Path | None:
    handle, data = reqenv.getenv("ORCA_TERMINAL_HANDLE"), reqenv.getenv("ORCA_USER_DATA_PATH")
    return dispatched_coordinator(handle, session_id, Path(data)) if handle and data and session_id else None


@ttl_cache(DISPATCH_TTL)
def dispatched_coordinator(handle: str, session_id: str, user_data: Path) -> Path | None:
    if (panes := dispatch_panes(handle)) is None:
        return None
    worker, creator = panes
    entries = pane_entries(user_data)
    if (own := claude_session(entries.get(worker))) is None or own[0] != session_id:
        return None
    return None if (coordinator := claude_session(entries.get(creator))) is None else coordinator[1]


def dispatch_panes(handle: str) -> tuple[str, str] | None:
    from captain_hook.builtin_packs.general.hooks._sessions import orca_json

    checked = ("orca", "orchestration", "check", "--terminal", handle, "--peek", "--json")
    if not isinstance(dispatch := orca_json(checked, "result", "dispatchId"), str):
        return None
    shown = ("orca", "orchestration", "worker-show", "--dispatch", dispatch, "--json")
    if not isinstance(task := orca_json(shown, "result", "dispatch", "taskId"), str):
        return None
    match orca_json(("orca", "orchestration", "dispatch-show", "--task", task, "--json"), "result", "dispatch"):
        case {
            "id": found,
            "assignee_handle": assignee,
            "status": "dispatched",
            "assignee_pane_key": str() as worker,
            "creator_pane_key": str() as creator,
        } if found == dispatch and assignee == handle:
            return worker, creator
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
