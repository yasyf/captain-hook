from __future__ import annotations

from captain_hook import Allow, Content, Input, Tool, UsedSkill, Warn, nudge

nudge(
    "You're editing LLM prompt content. The `llm-prompts` skill covers positive framing, "
    "XML structure, the prompting principles, and current per-provider model behaviors "
    "(Claude, GPT-5.x, Gemini) — consult it before writing prompts. After editing, run "
    "`/slop-cop-check` on the file to surface LLM-generated writing tells (overused "
    "intensifiers, hedge stacks, em-dash pivots, throat-clearing) and revise any real hits.",
    only_if=[Tool("Edit|Write"), Content(PROMPT_MARKERS, project_only=False)],
    skip_if=[
        UsedSkill("llm-prompts"),
        UsedSkill("slop-cop-check", "slop-cop-prose"),
    ],
    tests={
        Input(
            file="agent.py", content='messages = [{"role": "system", "content": "You are a helpful assistant."}]\n'
        ): Warn(),
        Input(file="prompt.md", content="<instruction>\nSummarize the document.\n</instruction>\n"): Warn(),
        Input(file="util.py", content="def add(a, b):\n    return a + b\n"): Allow(),
    },
)
