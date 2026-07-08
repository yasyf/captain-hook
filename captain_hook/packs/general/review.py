from __future__ import annotations

from captain_hook import Allow, Block, Event, Input, llm_gate
from captain_hook.packs.general._lib import EditedSource

llm_gate(
    "You are reviewing a code change before the agent stops. The compact diff of the "
    "uncommitted changes is in <diff>; it is the ONLY review subject. Review it for "
    "(1) correctness bugs and (2) clear violations of the project's STYLEGUIDE.md "
    "(read STYLEGUIDE.md from the working dir). Scope rules, applied before any finding: "
    "review only code that appears in <diff> — the transcript is context for understanding "
    "what the change is trying to do, never an additional review subject; files outside the "
    "repository working tree (session scratchpads, temp directories, one-shot workflow "
    "continuation scripts) are never review subjects, even when the transcript shows them "
    "being written; a deliberate guard or tripwire in an already-completed one-shot script "
    "is not a correctness bug. Set block=true ONLY for a concrete, real issue in the changed "
    "code shown in <diff>, with the specific problem and the fix in `reasoning`. Otherwise "
    "block=false. Do not block on style nits absent from STYLEGUIDE, on unchanged "
    "pre-existing code, or on speculative concerns.",
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
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "id": "e1",
                                "input": {
                                    "file_path": (
                                        "/tmp/claude-scratch/wf_0be55dd2/r4-judge-continuation-wf_0be55dd2-432.js"
                                    ),
                                    "content": "if (input.judgePrompt.length < 1000) throw new Error('tripwire')",
                                },
                            }
                        ]
                    },
                },
            ]
        ): Allow(),
    },
)
