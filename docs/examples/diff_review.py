"""Review the working-tree diff for leftover debugging artifacts before the agent stops."""

from __future__ import annotations

from captain_hook import Allow, Block, Event, Input, T, TouchedFile, llm_gate

llm_gate(
    "The working-tree diff is in <diff>. Did this change leave debugging artifacts in the "
    "code, such as a stray print/console.log/debugger, a commented-out block, or a "
    "temporary TODO marked for removal? Block only if the diff clearly adds one.",
    message=(
        "The diff leaves debugging artifacts behind. "
        "Remove the stray prints, commented-out blocks, and temporary TODOs before stopping."
    ),
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
