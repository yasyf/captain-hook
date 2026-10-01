"""Shared test fixtures for the general pack's hook modules; the ``_`` prefix keeps the loader from registering it."""

from __future__ import annotations

from captain_hook import T

SCRATCH_WORKFLOW_WRITE_FIXTURE = [
    T.assistant(
        T.tool(
            "Write",
            file_path="/tmp/claude-scratch/workflow/judge-continuation.js",
            content="if (input.judgePrompt.length < 1000) throw new Error('tripwire')",
        )
    ),
]
