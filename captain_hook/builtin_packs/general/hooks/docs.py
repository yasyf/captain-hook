from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    EditedSource,
    Event,
    FilePath,
    Headless,
    Input,
    T,
    Tool,
    TouchedFile,
    UsedSkill,
    Warn,
    llm_gate,
    nudge,
)
from captain_hook.builtin_packs.general.hooks._lib import SCRATCH_WORKFLOW_WRITE_FIXTURE

nudge(
    "Documentation edits go through the `writing-docs` skill, which covers the voice rules and the "
    "`slop-cop` check. Run `/writing-docs` before editing.",
    only_if=[Tool("Write|Edit"), FilePath("**/*.md", "**/*.qmd", "**/docs/**", "README.md")],
    skip_if=[UsedSkill("writing-docs", scope="session")],
    max_fires=1,
    tests={
        Input(tool="Write", file="docs/guide/x.qmd", content="# X"): Warn(pattern="writing-docs"),
        Input(tool="Write", file="packages/cli/docs/cheatsheet.txt", content="# X"): Warn(pattern="writing-docs"),
        Input(tool="Edit", file="src/app.py", content="x = 1"): Allow(),
        Input(
            tool="Edit",
            file="docs/guide/y.qmd",
            content="# Y",
            transcript=[
                T.user("write the guide"),
                T.assistant(T.tool("Skill", skill="writing-docs")),
                T.user("now the other page"),
                T.assistant("ok"),
            ],
        ): Allow(),
    },
)


llm_gate(
    "You are checking documentation freshness before the agent stops. The compact diff of "
    "the uncommitted changes is in <diff>. Judge only the change shown in <diff>; the "
    "transcript is context for intent, and files outside the repository working tree are "
    "never in scope. Decide whether the session changed anything "
    "user-facing — a new flag or option, a renamed command, changed output or behavior, a "
    "new feature — that README.md or the pages under docs/ don't reflect. Set block=true "
    "ONLY for a concrete gap, naming the file and section in `reasoning`. Otherwise block=false. "
    "Do not block on internal refactors, test or tooling changes, or speculative staleness.",
    message="A user-facing change is missing from README.md or docs/. Run `/writing-docs` and update the page.",
    diff=True,
    only_if=[EditedSource()],
    skip_if=[
        TouchedFile("**/*.md", "**/*.qmd"),
        UsedSkill("writing-docs", scope="session"),
        Headless(),
    ],
    events=Event.Stop,
    max_fires=1,
    tests={
        Input(transcript=[T.assistant(T.tool("Edit", file_path="src/app.py", old_string="a", new_string="b"))]): Block(
            pattern="writing-docs"
        ),
        Input(
            transcript=[
                T.assistant(
                    T.tool("Edit", file_path="src/app.py", old_string="a", new_string="b"),
                    T.tool("Edit", file_path="docs/index.md", old_string="a", new_string="b"),
                )
            ]
        ): Allow(),
        Input(transcript=[T.assistant(T.tool("Edit", file_path="README.md", old_string="a", new_string="b"))]): Allow(),
        Input(
            transcript=[T.assistant(T.tool("Edit", file_path="tests/test_app.py", old_string="a", new_string="b"))]
        ): Allow(),
        Input(
            transcript=[
                T.user("write the guide"),
                T.assistant(T.tool("Skill", skill="writing-docs")),
                T.user("now refactor the app"),
                T.assistant(T.tool("Edit", file_path="src/app.py", old_string="a", new_string="b")),
            ]
        ): Allow(),
        Input(transcript=SCRATCH_WORKFLOW_WRITE_FIXTURE): Allow(),
    },
)
