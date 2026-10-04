from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from captain_hook import Allow, Event, Input, LambdaCondition, Tool, on
from captain_hook.builtin_packs.general.hooks._sessions import (
    INLINE_SESSION,
    SpawnRecord,
    Spawns,
    Unreadable,
    backgrounds,
    read_ownership,
    spawned_rows,
    utc_now,
)

if TYPE_CHECKING:
    from captain_hook import BaseHookEvent, HookResult

spawn_recorder = partial(on, only_if=[Tool("Bash"), LambdaCondition(backgrounds)])


@spawn_recorder(
    Event.PreToolUse,
    tests={
        Input(command="nohup sleep 300 >/dev/null 2>&1 &", session_id=INLINE_SESSION): Allow(),
        Input(command="sleep 1 && echo done", session_id=INLINE_SESSION): Allow(),
    },
)
def note_background_launch(evt: BaseHookEvent) -> HookResult | None:
    if evt.tool_use_id is not None:
        with Spawns.mutate(evt) as spawns:
            spawns.pending[evt.tool_use_id] = utc_now()
    return None


@spawn_recorder(
    Event.PostToolUse | Event.PostToolUseFailure,
    tests={
        Input(command="nohup sleep 300 >/dev/null 2>&1 &", session_id=INLINE_SESSION): Allow(),
        Input(command="sleep 300 &", session_id=INLINE_SESSION, error="Exit code 1"): Allow(),
    },
)
def record_background_spawns(evt: BaseHookEvent) -> HookResult | None:
    if evt.tool_use_id is None or (since := Spawns.load(evt).pending.get(evt.tool_use_id)) is None:
        return None
    ownership = read_ownership()
    names = frozenset(call.name for call in evt.cmd.calls())
    with Spawns.mutate(evt) as spawns:
        del spawns.pending[evt.tool_use_id]
        if isinstance(ownership, Unreadable):
            return None
        rows = ownership.table.rows
        spawns.records = {
            key: record
            for key, record in spawns.records.items()
            if (row := rows.get(record.pid)) is not None and record.matches(row)
        } | {
            (record := SpawnRecord.of(row, evt.agent_id or "main")).key: record
            for row in spawned_rows(ownership, since, names, evt.session_id)
        }
    return None
