from __future__ import annotations

from captain_hook import Allow, BaseHookEvent, CustomCondition, Event, FilePath, Input, TestFile, Tool, Warn, hook
from captain_hook.hook_lint import lint_source


class MissesAuthoringBar(CustomCondition):
    def check(self, evt: BaseHookEvent) -> bool:
        match evt.file, evt.old, evt.content:
            case None, _, _:
                return False
            case file, None, str() as written:
                return bool(lint_source(file.path, written))
            case file, _, _:
                return bool(lint_source(file.path, file.path.read_text()))


hook(
    Event.PostToolUse,
    message=(
        "Hook files meet the authoring-hooks skill's bar. "
        "Run `uvx --isolated capt-hook lint <file>` and fix every finding."
    ),
    only_if=[
        Tool.EditTools,
        FilePath(
            ".claude/hooks/*.py",
            "**/.claude/hooks/*.py",
            "capt-hook/hooks/*.py",
            "**/capt-hook/hooks/*.py",
            "**/builtin_packs/*/hooks/*.py",
        ),
        MissesAuthoringBar(),
    ],
    skip_if=[TestFile()],
    tests={
        Input(
            tool="Write",
            file=".claude/hooks/uv_not_pip.py",
            content="nudge(\"User feedback: 'stop using pip, uv only'. Run `uv add`.\")\n",
        ): Warn(pattern="capt-hook lint"),
        Input(
            tool="Write",
            file=".claude/hooks/uv_not_pip.py",
            content='nudge("This repo installs with uv. Run `uv add <pkg>`.")\n',
        ): Allow(),
        Input(
            tool="Write",
            file="plugin/capt-hook/hooks/guards.py",
            content='nudge("Per R624 the owner said no. Use ccx.")\n',
        ): Warn(pattern="capt-hook lint"),
        Input(tool="Write", file="src/app.py", content="# narration\n"): Allow(),
    },
)
