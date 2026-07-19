# Bypass-launched sessions still pop scratch-write dialogs (plan-mode override, forwarded
# subagent dialogs); answered at both events like teammate_permissions.py. Design: cc-notes 94e5fed5.
from __future__ import annotations

from captain_hook import (
    Allow,
    Ask,
    Input,
    ScratchPath,
    SkipPermissions,
    Tool,
    approve,
)
from captain_hook.packs.fixes._lib import McpTool

approve(
    "scratch-dir writes under skip-permissions",
    only_if=[Tool("Edit|Write|MultiEdit|NotebookEdit"), ScratchPath(), SkipPermissions()],
    skip_if=[McpTool()],
    tests={
        Input(file="/tmp/sweep_arc.py", content="print(1)", skip_permissions=True): Allow(explicit=True),
        Input(file="/tmp/x.py", content="x", agent_id="tm1", skip_permissions=True): Allow(explicit=True),
        Input(file="/private/tmp/conf.py", old="a", content="b", skip_permissions=True): Allow(explicit=True),
        Input(file="/var/folders/ab/cd/T/scratch.json", content="{}", skip_permissions=True): Allow(explicit=True),
        Input(file="../../../../tmp/sweep_arc.py", content="print(1)", cwd="/a/b/c/d", skip_permissions=True): Allow(
            explicit=True
        ),
        Input(file="/Users/u/proj/scratchpads/notes.md", content="x", skip_permissions=True): Allow(explicit=True),
        Input(file="/Users/u/proj/scratch/plan.py", content="x", skip_permissions=True): Allow(explicit=True),
        Input(
            tool="NotebookEdit",
            tool_input={"notebook_path": "/tmp/nb.ipynb", "new_source": "x"},
            skip_permissions=True,
        ): Allow(explicit=True),
        Input(file="/tmp/../Users/u/proj/main.py", content="x", skip_permissions=True): Ask(),  # realpath kills spoof
        Input(file="../../tmp/x.py", content="x", skip_permissions=True): Ask(),  # relative, no cwd: unresolvable
        Input(file="/Users/u/proj/src/main.py", content="x", skip_permissions=True): Ask(),
        Input(file="/Users/u/proj/tmp", content="x", skip_permissions=True): Ask(),  # a *file* named tmp
        Input(file="/tmp/x.py", content="x", skip_permissions=False): Ask(),  # no consent
        Input(
            tool="mcp__srv__Write", tool_input={"file_path": "/tmp/x.py", "content": "x"}, skip_permissions=True
        ): Ask(),
    },
)
