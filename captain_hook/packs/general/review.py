from __future__ import annotations

from captain_hook import Allow, BaseHookEvent, Block, CustomCondition, Event, Input, Waiting, gate

# Prose and config file extensions that shouldn't, on their own, demand a code-review pass.
# Tailor this (and the excluded dirs below) to scope what counts as "source" for your repo.
NON_SOURCE_SUFFIXES = (
    ".md", ".mdx", ".rst", ".txt", ".json", ".toml",
    ".yaml", ".yml", ".ini", ".cfg", ".lock",
)


class EditedSource(CustomCondition):
    """True when the session edited a non-test source file (docs and config excluded)."""

    def check(self, evt: BaseHookEvent) -> bool:
        return any(
            not f.is_test
            and f.suffix not in NON_SOURCE_SUFFIXES
            and not f.under("docs", ".claude", ".github")
            for f in evt.ctx.t.tool_calls.named("Edit|Write").files()
        )


gate(
    "You changed source files but haven't done a review pass. Before stopping, review your "
    "changes for correctness and against STYLEGUIDE.md, and fix any issues in the code you "
    "wrote. See: STYLEGUIDE.md.",
    only_if=[EditedSource()],
    skip_if=[Waiting()],
    events=Event.Stop,
    tests={
        Input(transcript=[
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "id": "e1",
                 "input": {"file_path": "src/app.py", "old_string": "a", "new_string": "b"}}]}},
        ]): Block(),
        Input(transcript=[
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "id": "e1",
                 "input": {"file_path": "README.md", "old_string": "a", "new_string": "b"}}]}},
        ]): Allow(),
    },
)
