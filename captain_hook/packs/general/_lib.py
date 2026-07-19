"""Shared test fixtures for the general pack's hook modules; the ``_`` prefix keeps the loader from registering it."""

from __future__ import annotations

# A Write to a workflow scratch script (outside the repo working tree) containing a deliberate
# tripwire — shared by review.py and docs.py, both of which must never treat it as a review subject.
SCRATCH_WORKFLOW_WRITE_FIXTURE = [
    {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Write",
                    "id": "e1",
                    "input": {
                        "file_path": ("/tmp/claude-scratch/wf_0be55dd2/r4-judge-continuation-wf_0be55dd2-432.js"),
                        "content": "if (input.judgePrompt.length < 1000) throw new Error('tripwire')",
                    },
                }
            ]
        },
    },
]
