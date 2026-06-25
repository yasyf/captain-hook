"""Review the working-tree diff for leftover debugging artifacts before the agent stops."""

from __future__ import annotations

from captain_hook import Event, SourceEdits, llm_gate

# `diff=True` attaches a compact `ccx diff` (a plain `git diff` when ccx is absent) as a
# <diff> block, so the model reviews the actual change instead of reconstructing it from
# the transcript. A cheap source-edit filter gates the call, and it fires at most once
# before the agent stops.
llm_gate(
    "The working-tree diff is in <diff>. Did this change leave debugging artifacts in the "
    "code, such as a stray print/console.log/debugger, a commented-out block, or a "
    "temporary TODO marked for removal? Block only if the diff clearly adds one.",
    message=lambda r: f"Remove the debugging leftovers before stopping: {r.reasoning}",
    diff=True,
    only_if=[SourceEdits()],
    events=Event.Stop,
    model="small",
    max_fires=1,
)
