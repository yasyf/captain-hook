from __future__ import annotations

from captain_hook import Allow, EditedSource, Event, Input, T, Warn, llm_nudge
from captain_hook.builtin_packs.general.hooks._lib import SCRATCH_WORKFLOW_WRITE_FIXTURE

llm_nudge(
    "You are reviewing a code change before the agent stops. The compact diff of the "
    "uncommitted changes is in <diff>; it is the ONLY review subject. Review it for "
    "(1) correctness bugs and (2) clear violations of the project's STYLEGUIDE.md "
    "(read STYLEGUIDE.md from the working dir). Scope rules, applied before any finding: "
    "review only code that appears in <diff> — the transcript is context for understanding "
    "what the change is trying to do, never an additional review subject; files outside the "
    "repository working tree (session scratchpads, temp directories, workflow scripts) are "
    "never review subjects, even when the transcript shows them being written; a deliberate "
    "guard or tripwire in a completed script is not a correctness bug. Set fire=true ONLY "
    "for a concrete, real issue in the changed code shown in <diff>, with the specific "
    "problem and the fix in `reasoning`. Otherwise fire=false. Do not fire on style nits "
    "absent from STYLEGUIDE, on unchanged pre-existing code, or on speculative concerns.",
    message=(
        "The stop review found a correctness bug or STYLEGUIDE.md violation in the uncommitted diff. "
        "Run `git diff` and fix the issue."
    ),
    diff=True,
    only_if=[EditedSource()],
    events=Event.Stop,
    max_fires=1,
    tests={
        Input(transcript=[T.assistant(T.tool("Edit", file_path="src/app.py", old_string="a", new_string="b"))]): Warn(),
        Input(transcript=[T.assistant(T.tool("Edit", file_path="README.md", old_string="a", new_string="b"))]): Allow(),
        Input(transcript=SCRATCH_WORKFLOW_WRITE_FIXTURE): Allow(),
    },
)
