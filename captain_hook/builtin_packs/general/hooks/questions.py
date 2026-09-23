from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    Event,
    Input,
    RanCommand,
    Regex,
    Signal,
    Signals,
    T,
    UsedSkill,
    UsedTool,
    llm_gate,
)

O1_PROSE_DECISION = (
    "One decision for you, O1: the IMDS firewall is what forces three hand-rolled scripts. Keep it, and an "
    "unconfigured job fails instead of silently writing as the instance role; drop it after the Elastic work "
    "lands and stock tags replace all three. The lane recommends keep now, drop as a follow-up, and is "
    "proceeding on keep."
)

llm_gate(
    """You are a senior engineer watching another engineer ("the agent") end its turn. Your
one job: decide whether the agent's closing message puts a question or decision to the
user in PROSE instead of asking it with the AskUserQuestion tool.

The user's standing rule: when work needs the user's decision, the question IS an
AskUserQuestion call (2-4 concrete options, recommended first) in that same turn. A turn
that ends on the question written as text, and then sits idle or proceeds on a default,
is a bug. This covers every shape of it. Background work still running is not an excuse
to defer the question — a background task's result is never the user's answer.

Prose-question tells (lean block=true): "one decision for you", "holding for your pick",
"still yours to decide", "your call", "your decision", "up to you", "say the word",
"I'll leave it to you", "needs your sign-off", "let me know which", "tell me which",
"if you'd rather", "which do you prefer", an A-or-B fork laid out as options, a deferral
list parked under a heading such as "Still yours to decide:", "the lane recommends X and
is proceeding on X" (a decision the user never got a real prompt for), "want me to ...?"
or "should I ...?" offers closing the message.

Do NOT fire when: the question is rhetorical and the agent answers it itself; the agent
quotes or reports someone else's question; the question is addressed to a subagent,
teammate, or tool rather than the user; the message reports finished work with no open
decision; the agent already called AskUserQuestion or ExitPlanMode for it; or the decision
is already presented on a live cc-present board (a `present` skill or `cc-present start` in
this session) and the prose refers the user to it.

When uncertain, return block=false. Put your reasoning (under 40 words, quoting the
prose question) in `reasoning`.""",
    message=(
        "You put a question to the user in prose: {reasoning} "
        "Ask it with AskUserQuestion (2-4 concrete options, recommended first) in this turn "
        "instead of ending on it."
    ),
    label="prose_question_to_user",
    signals=Signals(
        [
            Signal(pattern=r"(?i)\bone decision for you\b", weight=2),
            Signal(pattern=r"(?i)\b(?:holding|waiting) (?:for|on) (?:your|you)\b", weight=2),
            Signal(pattern=r"(?i)\b(?:let me know|tell me) (?:which|what|if|whether|how)\b", weight=2),
            Signal(pattern=r"(?i)\byour (?:call|pick|decision|choice|move|shout)\b", weight=2),
            Signal(pattern=r"(?i)\byours? to (?:decide|call|choose)\b", weight=2),
            Signal(pattern=r"(?i)\bup to you\b", weight=2),
            Signal(pattern=r"(?i)\bsay the word\b", weight=2),
            Signal(pattern=r"(?i)\bif you(?:'|’)?d (?:rather|prefer)\b", weight=2),
            Signal(pattern=r"(?i)\bleave (?:it|that|this|them) (?:up )?to you\b", weight=2),
            Signal(pattern=r"(?i)\bneeds? your (?:call|decision|input|sign-?off)\b", weight=2),
            Signal(pattern=r"(?i)\bwhich (?:one )?(?:do|would) you (?:want|prefer|like)\b", weight=2),
            Signal(pattern=r"(?i)\b(?:should I|want me to|shall I)\b[^.?!]*[.?!]", weight=2),
            Signal(pattern=r"(?i)\brecommends?\b[^.]*\b(?:proceeding|going ahead) (?:on|with)\b", weight=2),
            Signal(pattern=r"\?\s*$", weight=1),
            Signal(pattern=r"(?im)^\s*(?:[-*]\s*)?(?:option\s+[A-D1-4]\b|\(?[a-d]\)\s)", weight=1),
        ],
        threshold=2,
        window=6,
        scope="text",
    ),
    skip_if=[
        UsedTool("AskUserQuestion", "ExitPlanMode"),
        UsedSkill("present", scope="session"),
        RanCommand(Regex(r"^(?:\S*/)?cc-present start\b"), subagents=True),
    ],
    guards_waiting=False,
    events=Event.Stop,
    tests={
        Input(transcript=[T.assistant(O1_PROSE_DECISION)]): Block(pattern="AskUserQuestion"),
        Input(transcript=[T.assistant("Holding for your pick: rebase onto dev or cherry-pick the fix?")]): Block(
            pattern="AskUserQuestion"
        ),
        Input(
            transcript=[
                T.assistant(
                    "Both stacks are green and the boot stack is four PRs deep.\n\n"
                    "Still yours to decide at the end: the api-actions pool PRs, #21840 and #21847."
                )
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[T.assistant("Still yours to decide: the api-actions pool PRs, #21840 and #21847.")],
            background_tasks=[
                {"id": "t1", "type": "subagent", "status": "running", "description": "pr-watcher on #21840"}
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.assistant("Still yours to decide: the api-actions pool PRs, #21840 and #21847."),
                *(T.assistant(T.tool("Read", file_path=f"api/src/f{n}.ts")) for n in range(8)),
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[T.assistant("The cap is 30 slots today — up to you whether we raise it or shed jobs.")]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.assistant(O1_PROSE_DECISION),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Keep the IMDS firewall?"}])),
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(T.tool("Skill", skill="cc-present:present")),
                T.assistant(
                    "Board is live at http://localhost:4173; if you'd rather skip the board, say 'use defaults'."
                ),
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    T.tool(
                        "Bash",
                        command="/Users/me/.claude/plugins/cache/cc-present/bin/cc-present start --session x --doc y",
                    )
                ),
                T.assistant("Your call on the board."),
            ]
        ): Allow(),
        Input(transcript=[T.assistant("Shipped #94; CI is green.")]): Allow(),
        Input(
            transcript=[T.assistant("Both pool PRs are merged; nothing is left to decide.")],
            llm={"block": False},
        ): Allow(),
        Input(
            transcript=[T.assistant("Why did the build fail? Let me know which key went stale: the lockfile.")],
            llm={"block": False},
        ): Allow(),
    },
)
