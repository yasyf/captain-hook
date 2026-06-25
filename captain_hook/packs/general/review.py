from __future__ import annotations

from captain_hook import Allow, BaseHookEvent, Block, CustomCondition, Event, Input, llm_gate

# Prose and config file extensions that shouldn't, on their own, demand a code-review pass.
# Tailor this (and the excluded dirs below) to scope what counts as "source" for your repo.
NON_SOURCE_SUFFIXES = (
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".lock",
)


class EditedSource(CustomCondition):
    """True when the session edited a non-test source file (docs and config excluded)."""

    def check(self, evt: BaseHookEvent) -> bool:
        return any(
            not f.is_test and f.suffix not in NON_SOURCE_SUFFIXES and not f.under("docs", ".claude", ".github")
            for f in evt.ctx.t.tool_calls.named("Edit|Write").files()
        )


llm_gate(
    "You are reviewing a code change before the agent stops. The compact diff of the "
    "uncommitted changes is in <diff>. Review it for (1) correctness bugs and (2) clear "
    "violations of the project's STYLEGUIDE.md (read STYLEGUIDE.md from the working dir). "
    "Set block=true ONLY for a concrete, real issue in the changed code, with the specific "
    "problem and the fix in `reasoning`. Otherwise block=false. Do not block on style nits "
    "absent from STYLEGUIDE, on unchanged pre-existing code, or on speculative concerns.",
    message=lambda r: f"Review flagged an issue to fix before stopping: {r.reasoning}",
    diff=True,
    only_if=[EditedSource()],
    events=Event.Stop,
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
        ): Block(),
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
    },
)
