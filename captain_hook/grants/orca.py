"""An Orca lane reads its Run coordinator's grants, bound through Orca's own orchestration record."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, Field

from captain_hook.grants import store
from captain_hook.grants.evidence import tree_of
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

ORCA_TIMEOUT = 5
BINDING_TTL = timedelta(minutes=1)
WORKER_PAGE = 100


class OrcaRun(BaseModel):
    terminal: str | None = None
    run: str | None = None
    coordinator: str | None = None
    checked: datetime | None = None
    pinned: dict[str, str] = Field(default_factory=dict[str, str])


def terminal() -> str | None:
    return reqenv.getenv("ORCA_TERMINAL_HANDLE") or None


def attended() -> bool:
    return reqenv.getenv("CLAUDE_CODE_SESSION_ATTENDED") == "1"


def orca(*args: str) -> dict[str, Any] | None:
    try:
        done = subprocess.run(
            ["orca", *args, "--json"],
            capture_output=True,
            text=True,
            timeout=ORCA_TIMEOUT,
            env=reqenv.env_map(),
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    return json.loads(done.stdout)["result"]


def dispatched_run(handle: str) -> str | None:
    cursor: list[str] = []
    while (page := orca("orchestration", "worker-list", "--limit", str(WORKER_PAGE), *cursor)) is not None:
        for worker in page["workers"]:
            if worker["agentTerminalHandle"] == handle and worker["dispatchStatus"] == "dispatched":
                return worker["runId"]
        if not page["page"]["hasMore"]:
            return None
        cursor = ["--cursor", page["page"]["nextCursor"]]
    return None


def resolve(handle: str) -> tuple[str | None, str | None]:
    if (run := dispatched_run(handle)) is None or (shown := orca("orchestration", "run-show", "--id", run)) is None:
        return None, None
    return run, shown["run"]["coordinator_handle"]


def pin(run: str | None, coordinator: str | None) -> str:
    return f"{run} {coordinator}"


def bound_run(evt: BaseHookEvent, handle: str) -> OrcaRun:
    slot = evt.ctx.session[OrcaRun]
    at = store.now()
    cached = slot.get(OrcaRun())
    if cached.terminal == handle and cached.checked is not None and at - cached.checked < BINDING_TTL:
        return cached
    run, coordinator = resolve(handle)
    pinned = cached.pinned
    if run is not None and coordinator is not None and pin(run, coordinator) not in pinned:
        if (tree := store.terminal_tree(coordinator)) is not None:
            pinned = pinned | {pin(run, coordinator): tree}
    found = OrcaRun(terminal=handle, run=run, coordinator=coordinator, checked=at, pinned=pinned)
    slot.set(found)
    return found


def coordinator_tree(binding: OrcaRun) -> str | None:
    if binding.coordinator is None or (pinned := binding.pinned.get(pin(binding.run, binding.coordinator))) is None:
        return None
    return pinned if store.terminal_tree(binding.coordinator) == pinned else None


def record_terminal(evt: BaseHookEvent) -> None:
    if (handle := terminal()) is None or not attended() or not evt.ctx.session.once(handle, scope="orca-terminal"):
        return
    if store.record_terminal(handle, tree_of(evt)):
        logger.bind(terminal=handle, tree=tree_of(evt)).info("recorded the orca terminal's session tree")


def adopt_coordinator(evt: BaseHookEvent) -> None:
    if (handle := terminal()) is None:
        return
    binding = bound_run(evt, handle)
    tree = tree_of(evt)
    if (coordinator := coordinator_tree(binding)) is None or coordinator == tree:
        return
    agent = f"orca:{binding.run}"
    if adopted := store.adopt_tree(coordinator, tree=tree, session=evt.session_id, agent=agent):
        logger.bind(run=binding.run, coordinator=coordinator, tree=tree, adopted=adopted).info(
            "adopted the orca coordinator's grants"
        )
