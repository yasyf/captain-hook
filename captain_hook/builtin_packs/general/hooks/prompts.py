from __future__ import annotations

from captain_hook import Allow, Content, Input, T, Tool, UsedSkill, Warn, nudge

nudge(
    "LLM prompt content follows the `llm-prompts` skill and passes `/slop-cop-check`. "
    "Run `/slop-cop-check <file>` on this file and revise its real hits.",
    only_if=[
        Tool("Edit|Write"),
        Content(
            r"<instruction>|<system>|<examples>|<success_criteria>|<output_format>|"
            r"<key_constraints>|<reasoning_framework>|<action_rules>|<preferred_patterns>|"
            r"<persona>|<role>|<tool_persistence>|<completeness_contract>|<verification_loop>|"
            r"You are an?\b|Your task is to\b|You will be provided with\b|"
            r"def\s+\w*prompt\s*\(|(?i:\b(?:system|developer) prompt\b)|"
            r"messages\s*=\s*\[|"
            r"""["']role["']\s*:\s*["'](?:system|user|assistant|developer)["']""",
            project_only=False,
        ),
    ],
    skip_if=[
        UsedSkill("llm-prompts", scope="session"),
        UsedSkill("slop-cop-check", "slop-cop-prose", scope="session"),
        Content(r"\.count_tokens\(", project_only=False),
    ],
    tests={
        Input(
            file="agent.py", content='messages = [{"role": "system", "content": "You are a helpful assistant."}]\n'
        ): Warn(),
        Input(file="prompt.md", content="<instruction>\nSummarize the document.\n</instruction>\n"): Warn(),
        Input(file="util.py", content="def add(a, b):\n    return a + b\n"): Allow(),
        Input(
            file="prompt.md",
            content="<instruction>\nSummarize the document.\n</instruction>\n",
            transcript=[
                T.user("write the prompt"),
                T.assistant(T.tool("Skill", skill="llm-prompts")),
                T.user("now the second prompt"),
                T.assistant("ok"),
            ],
        ): Allow(),
        Input(
            file="tokens.py",
            content=(
                "def api_count(text: str, model: str) -> int:\n"
                "    resp = client.messages.count_tokens(\n"
                '        model=model,\n        messages=[{"role": "user", "content": text or " "}],\n'
                "    )\n    return int(resp.input_tokens)\n"
            ),
        ): Allow(),
    },
)
