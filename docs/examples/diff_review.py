"""Review the working-tree diff for leftover debugging artifacts before the agent stops."""

from __future__ import annotations

from captain_hook import Allow, Block, Event, Input, T, TouchedFile, llm_gate

# `diff=True` attaches a compact `ccx vcs diff` (a plain `git diff` when ccx is absent) as a
# <diff> block, so the model reviews the actual change instead of reconstructing it from
# the transcript. Stop carries no tool input, so the cheap pre-filter is the
# transcript-history condition TouchedFile — "did this session edit Python source?" —
# and the gate fires at most once before the agent stops.
llm_gate(
    "The working-tree diff is in <diff>. Did this change leave debugging artifacts in the "
    "code, such as a stray print/console.log/debugger, a commented-out block, or a "
    "temporary TODO marked for removal? Block only if the diff clearly adds one.",
    message="Remove the debugging leftovers before stopping: {reasoning}",
    diff=True,
    only_if=[TouchedFile("**/*.py")],
    events=Event.Stop,
    model="small",
    max_fires=1,
    tests={
        Input(
            transcript=[T.assistant(T.tool("Edit", file_path="src/app.py", old_string="a", new_string="b"))]
        ): Block(),
        Input(transcript=[T.assistant(T.tool("Edit", file_path="README.md", old_string="a", new_string="b"))]): Allow(),
    },
)
