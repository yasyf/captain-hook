from __future__ import annotations

from captain_hook import (
    Agent,
    Content,
    Event,
    SourceEdits,
    TestFile,
    llm_gate,
)

# Keep LLM hooks cheap. Gate the model behind cheap deterministic filters, run the
# small model, fire at most once per session, and skip planning subagents. Most
# turns never reach the model, so they never spend a token.
llm_gate(
    "Does this edit weaken a test, turning a real assertion into assertTrue(True), a "
    "no-op mock, or a skip, to make a failing test pass? Block only if unambiguous.",
    message=lambda r: f"This looks like a weakened test: {r.reasoning}",
    # Cheap pre-filters first: only edits to a test file that actually touch
    # assert/mock/skip ever reach the model.
    only_if=[SourceEdits(lang="py", include_tests=True), TestFile(), Content(r"\b(assert|mock|skip)\b")],
    skip_if=[Agent("Explore|Plan|general-purpose")],
    events=Event.PostToolUse,
    model="small",
    max_fires=1,
)
