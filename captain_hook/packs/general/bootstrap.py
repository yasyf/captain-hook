from __future__ import annotations

from captain_hook import FilePath, TestFile, Tool, llm_nudge

# Path glob over-selects on "template"/"bootstrap" (including false positives like
# Bootstrap-CSS); the LLM judge is the real filter.
llm_nudge(
    "You are checking whether a file edit needs to propagate beyond this one repo. "
    "The edited file's before/after text is in <before_edit>/<after_edit>. Judge only "
    "whether the edited file (its path and content) is plausibly a CANONICAL script, "
    "config, or workflow template that other repos are instantiated or synced FROM -- a "
    "bootstrap/scaffold template, a fleet-distributed installer or CI script, or similar "
    "'this seeds copies elsewhere' artifact -- as opposed to an ordinary repo-local file "
    "with no other consumers (this also covers unrelated files that merely contain the "
    "word 'bootstrap', e.g. a Bootstrap-CSS asset -- those are never canonical sources). "
    "If it plausibly IS such a canonical/template source, set fire=true only when the "
    "edit itself gives no sign the agent already considered propagating the fix to repos "
    "already bootstrapped/instantiated from it (no mention of a sweep, sync, or fleet "
    "update). Otherwise set fire=false.",
    message=lambda r: (
        "User feedback (2026-07-08, session 9e5a15d4): \"1, but make the change in the "
        "templaet script and across all bootstrapepd repo as well where relevant\". "
        f"{r.reasoning} Fix the canonical template here, then sweep the same fix across "
        "already-bootstrapped/instantiated repos where relevant, not just this one."
    ),
    only_if=[
        Tool("Write|Edit"),
        FilePath("**/*template*", "**/*bootstrap*", "**/templates/**", "**/scaffold*/**"),
    ],
    skip_if=[TestFile()],
    max_fires=3,
)
