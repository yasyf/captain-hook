from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook.procwatch.identity import mismatch, start_unix
from captain_hook.util.proc import Ownership, Unreadable, children, is_agent, process_class

if TYPE_CHECKING:
    from captain_hook.procwatch.identity import ProcessIdentity
    from captain_hook.util.proc import ProcessRow, ProcessTable


@dataclass(frozen=True, slots=True)
class Owned:
    row: ProcessRow
    anchor: ProcessRow
    ancestry: tuple[ProcessRow, ...]


def descendants(table: ProcessTable, pid: int) -> tuple[ProcessRow, ...]:
    found: dict[int, ProcessRow] = {}
    frontier = [pid]
    while frontier:
        for child in children(table, frontier.pop()):
            if child.pid != pid and child.pid not in found:
                found[child.pid] = child
                frontier.append(child.pid)
    return tuple(found.values())


def lineage(table: ProcessTable, row: ProcessRow, anchor: ProcessRow) -> tuple[ProcessRow, ...] | None:
    chain = table.ancestors(row.pid)
    return next((chain[: depth + 1] for depth, entry in enumerate(chain) if entry.pid == anchor.pid), None)


def prove(
    table: ProcessTable, identity: ProcessIdentity, *, claude_pid: int, claude_start_unix: int
) -> Owned | Unreadable:
    if identity.pid <= 1:
        return Unreadable(f"pid {identity.pid} is not a user process.")
    if identity.pid == claude_pid:
        return Unreadable(f"pid {identity.pid} is this session's own agent process, which no session may signal.")
    if (anchor := table.rows.get(claude_pid)) is None:
        return Unreadable(f"this session's agent (pid {claude_pid}) is not in the process table.")
    if start_unix(anchor) != claude_start_unix:
        return Unreadable(f"pid {claude_pid} is no longer this session's agent: its start time differs.")
    if not is_agent(anchor):
        return Unreadable(f"pid {claude_pid} is not an agent process, so it cannot vouch for a child.")
    if identity.pid in {ancestor.pid for ancestor in table.ancestors(claude_pid)}:
        return Unreadable(f"pid {identity.pid} is an ancestor of this session, which no session may signal.")
    if (row := table.rows.get(identity.pid)) is None:
        return Unreadable(f"pid {identity.pid} is not in the process table.")
    if (why := mismatch(identity, row)) is not None:
        return why
    if (ancestry := lineage(table, row, anchor)) is None:
        return Unreadable(f"pid {identity.pid} does not descend from this session's agent (pid {claude_pid}).")
    if (nested := next((entry for entry in ancestry[:-1] if is_agent(entry)), None)) is not None:
        return Unreadable(f"pid {identity.pid} runs under a nested agent (pid {nested.pid}), which owns it.")
    if row.uid != (uid := os.getuid()):
        return Unreadable(f"pid {identity.pid} belongs to uid {row.uid}, not this user (uid {uid}).")
    if identity.pid in Ownership.resolve(table).protected:
        culprit = next(entry for entry in (row, *descendants(table, row.pid)) if process_class(entry) is not None)
        return Unreadable(
            f"pid {culprit.pid} is {process_class(culprit)}, which no session may signal, stop, or restart."
        )
    return Owned(row, anchor, ancestry)
