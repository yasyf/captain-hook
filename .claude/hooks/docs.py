from __future__ import annotations

from captain_hook import Allow, FilePath, Input, Tool, UsedSkill, Warn, nudge

# Advisory reminder to consult the writing-docs skill (and run slop-cop) before
# editing documentation. It fires once per session on the first doc edit and stands
# down once the skill has been used. Advisory only, so it never blocks an edit.
nudge(
    "You're editing documentation. Consult the writing-docs skill first for the "
    "Diataxis modes, voice rules, and code-sample rules, then run "
    "`slop-cop check <file> --markdown=on` to catch prose tells before you finish.",
    only_if=[Tool("Write|Edit"), FilePath("**/*.md", "**/*.qmd", "docs/**", "README.md")],
    skip_if=[UsedSkill("writing-docs|writing-docs:writing-docs")],
    max_fires=1,
    tests={
        Input(tool="Write", file="docs/guide/x.qmd", content="# X"): Warn(pattern="writing-docs"),
        Input(tool="Edit", file="captain_hook/cli.py", content="x = 1"): Allow(),
    },
)
