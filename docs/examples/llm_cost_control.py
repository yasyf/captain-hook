"""Gate an LLM verdict behind cheap deterministic filters to catch weakened tests cheaply."""

from __future__ import annotations

from captain_hook import (
    Agent,
    Content,
    Event,
    SourceEdits,
    TestFile,
    llm_gate,
)

llm_gate(
    "Does this edit weaken a test, turning a real assertion into assertTrue(True), a "
    "no-op mock, or a skip, to make a failing test pass? Block only if unambiguous.",
    message="Tests must keep their real assertions. Restore the assertion this edit weakened.",
    only_if=[SourceEdits(lang="py", include_tests=True), TestFile(), Content(r"\b(assert|mock|skip)\b")],
    skip_if=[Agent("Explore|Plan|general-purpose")],
    events=Event.PostToolUse,
    model="small",
    max_fires=1,
)
