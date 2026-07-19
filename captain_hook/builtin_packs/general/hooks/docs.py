from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    EditedSource,
    Event,
    FilePath,
    Headless,
    Input,
    Tool,
    TouchedFile,
    UsedSkill,
    Waiting,
    Warn,
    llm_gate,
    nudge,
)
from captain_hook.builtin_packs.general.hooks._lib import SCRATCH_WORKFLOW_WRITE_FIXTURE

# Advisory reminder to consult the writing-docs skill (and run slop-cop) before
# editing documentation. Fires once per session on the first doc edit and stands
# down once the skill has been used. Advisory only, so it never blocks an edit.
#
# The scaffolded .claude/settings.json registers the yasyf/cc-skills marketplace
# and enables writing-docs@skills, so the skill (and the skip_if check) activates
# when the folder is trusted — no manual /plugin install.
nudge(
    "You're editing documentation. Consult the writing-docs skill first for the "
    "Diataxis modes, voice rules, and code-sample rules, then run "
    "`slop-cop check <file> --lang=markdown` to catch prose tells before you finish. "
    "slop-cop is a Go binary — if it's not on PATH, run the `/slop-cop-check` skill "
    "(it installs it), never `uvx slop-cop`.",
    only_if=[Tool("Write|Edit"), FilePath("**/*.md", "**/*.qmd", "docs/**", "README.md")],
    skip_if=[UsedSkill("writing-docs")],
    max_fires=1,
    tests={
        Input(tool="Write", file="docs/guide/x.qmd", content="# X"): Warn(pattern="writing-docs"),
        Input(tool="Edit", file="src/app.py", content="x = 1"): Allow(),
    },
)


# Docs-freshness gate: after source edits, an LLM reads the uncommitted diff before the
# agent stops and blocks once when a user-facing change isn't reflected in README.md or
# docs/. Complements review.py's gate, which reviews correctness — this one reviews
# documentation. Stands down when the session already touched markdown/docs or used the
# writing-docs skill, and in headless (cron/CI) runs.
llm_gate(
    "You are checking documentation freshness before the agent stops. The compact diff of "
    "the uncommitted changes is in <diff>. Judge only the change shown in <diff>; the "
    "transcript is context for intent, and files outside the repository working tree are "
    "never in scope. Decide whether the session changed anything "
    "user-facing — a new flag or option, a renamed command, changed output or behavior, a "
    "new feature — that README.md or the pages under docs/ don't reflect. Set block=true "
    "ONLY for a concrete gap, naming exactly which file and section to update in "
    "`reasoning`. Otherwise block=false. Do not block on internal refactors, test or "
    "tooling changes, or speculative staleness.",
    message=lambda r: (
        f"Docs freshness check found a gap to close before stopping: {r.reasoning} "
        "Update README.md or docs/ via the writing-docs skill, or state that nothing "
        "user-facing changed and finish."
    ),
    diff=True,
    only_if=[EditedSource()],
    skip_if=[
        Waiting(),
        TouchedFile("**/*.md", "**/*.qmd"),
        UsedSkill("writing-docs|writing-docs:writing-docs"),
        Headless(),
    ],
    events=Event.Stop,
    max_fires=1,
    tests={
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "id": "e1",
                                "input": {"file_path": "src/app.py", "old_string": "a", "new_string": "b"},
                            }
                        ]
                    },
                },
            ]
        ): Block(pattern="writing-docs"),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "id": "e1",
                                "input": {"file_path": "src/app.py", "old_string": "a", "new_string": "b"},
                            },
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "id": "e2",
                                "input": {"file_path": "docs/index.md", "old_string": "a", "new_string": "b"},
                            },
                        ]
                    },
                },
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "id": "e1",
                                "input": {"file_path": "README.md", "old_string": "a", "new_string": "b"},
                            }
                        ]
                    },
                },
            ]
        ): Allow(),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "id": "e1",
                                "input": {"file_path": "tests/test_app.py", "old_string": "a", "new_string": "b"},
                            }
                        ]
                    },
                },
            ]
        ): Allow(),
        Input(transcript=SCRATCH_WORKFLOW_WRITE_FIXTURE): Allow(),
    },
)
