from __future__ import annotations

from captain_hook import Allow, Event, Input, Pattern, SourceEdits, Warn, hook

hook(
    Event.PostToolUse,
    only_if=[SourceEdits(lang="go"), Pattern("panic($$$)")],
    message="panic() aborts the whole process. Return an error so callers can recover; "
    "reserve panic for truly unrecoverable programmer mistakes.",
    tests={
        Input(tool="Edit", file="server/handler.go", content='func h() {\n\tpanic("boom")\n}\n'): Warn(pattern="error"),
        Input(tool="Edit", file="server/handler.go", content="func h() error {\n\treturn err\n}\n"): Allow(),
    },
)
