from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    Budget,
    CustomCondition,
    Event,
    FromSubagent,
    Input,
    RanCommand,
    Regex,
    Signal,
    Signals,
    T,
    UsedSkill,
    llm_gate,
)
from captain_hook.state import PrimitiveState

ASK_TOOLS = "AskUserQuestion|ExitPlanMode"
NO_COMMAND_FOR_YOU = r"(?!(?:(?!\n\n)[^.])*?[`\n]!\s)"

PROSE_DECISION = (
    "One decision for you: the firewall rule is what forces three hand-rolled scripts. Keep it, and an "
    "unconfigured job fails instead of silently writing as the instance role; drop it after the follow-up "
    "lands and stock tags replace all three. The lane recommends keep now, drop later, and is proceeding on keep."
)

WAITING_ON_YOU = (
    "The rollback is approved and dispatched but not yet confirmed done.\n"
    "\n"
    "**Open problems**\n"
    "- **Release 1.2:** it is live with an unsigned binary. One priority PR fixes the signing step.\n"
    "\n"
    "**Progress**\n"
    "- **Foundation stack:** the three foundation PRs landed on `dev`.\n"
    "\n"
    "**Waiting on you**\n"
    "- Read the design doc and lift its hold.\n"
    "- The two console clicks for the cut-over.\n"
    "\n"
    "The announcement stays held until every target has deployed green."
)

STILL_WITH_YOU = (
    "The landing desk confirms two PRs landed on `dev` after the rate-limit block lifted.\n"
    "\n"
    "**Problems**\n"
    "- **Ejection missed:** the queue ejected a PR on a red test and the watch had crashed on an "
    "over-long state filename. The lane has the fix routed.\n"
    "\n"
    "**Still with you:** the delete list and the design doc are both held for your read."
)

CONSOLE_CLICKS = (
    "The announcement is now held until every target has deployed green.\n"
    "\n"
    "- **What did not change:** the cut-over lanes still start as soon as the trust update is applied, "
    "and the post also waits on that cut-over and your two console clicks.\n"
    "\n"
    "A failed target comes to you straight away instead of waiting for the full table."
)

GO_GIVEN = (
    "**Phase 0 is nearly through:** the pipeline PR is green and approved, waiting only on the review "
    "check.\n"
    "\n"
    "I gave the lane its GO for what follows the landing:\n"
    "\n"
    "1. Create the pipeline.\n"
    "2. Plan the two stacks read-only; I approve only an update-only change, and any create, delete, "
    "or replace comes to you.\n"
    "\n"
    "Once it's merged and proven I'll post the usage note under your standing authorization."
)

OWED_REPORT = (
    "None of the targets has deployed through the new pipeline yet. They all live in one stack, where "
    "the first deploy was refused.\n"
    "\n"
    "**Why they are blocked:** the pipeline keeps one guard. A deploy that pins one target refuses if "
    "the plan changes any other row.\n"
    "\n"
    "**What I owe you and don't have:**\n"
    "- The detail on which updates change an image versus config only.\n"
    "- A per-target table for the whole catch-up order.\n"
    "\n"
    "That report is overdue. I've told the desk to return it within ten minutes."
)

APPROVAL_CONDITIONS = (
    "**I approved the account-id design for code, with conditions.** One small module reads the id "
    "from the root stack's state once, before any program runs.\n"
    "\n"
    "My conditions:\n"
    "- It reads through the engine's existing state access, not a hand-rolled path.\n"
    "- Any role grant it needs is a separate small PR, and that apply comes to you.\n"
    "\n"
    "The three scaffold PRs are green and go to the desk."
)

SSO_LOGIN = (
    "Both already handled (lanes stopped, tasks deleted). Nothing new. Waiting on your SSO login "
    "(`! aws sso login --sso-session forge` and `! aws login`); every lane that depends on it is polling "
    "and will act on its own once it succeeds."
)

DESK_WAKE = (
    'Another Claude session sent a message: <agent-message from="landing-desk"> Two PRs landed '
    "as squashes on dev. </agent-message>"
)


class AskedLast(CustomCondition):
    """True when the turn's last act was an ask tool call with no prose after it."""

    def check(self, evt: BaseHookEvent) -> bool:
        since = evt.ctx.t.after(tool=ASK_TOOLS)
        return evt.ctx.t.has_tool(ASK_TOOLS, subagents=False) and not (
            (count := len(since)) and since.assistant_text(count, max_per_msg=1)
        )


class ContinuingStop(CustomCondition):
    """True when this Stop continues a turn an earlier Stop block already extended."""

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.stop_hook_active


class AskToolDisallowed(CustomCondition):
    """True when the session was launched without the `AskUserQuestion` tool."""

    def check(self, evt: BaseHookEvent) -> bool:
        return "AskUserQuestion" in evt.disallowed_tools


llm_gate(
    """You are a senior engineer watching another engineer ("the agent") end its turn. Your
one job: decide whether the agent's closing message, its last prose in the transcript, leaves
something waiting on the user in PROSE instead of asking for it with the AskUserQuestion tool.

The user's standing rule: when work needs something only the user can give (a decision, an
answer, an approval, a read of a PR, design or delete list, a click or hand step, a "go"),
the ask IS an AskUserQuestion call (2-4 concrete options, recommended first) in that same
turn. A turn that ends on it written as text, then sits idle or proceeds on a default, is a
bug. Background work still running is no excuse to defer an item the user could act on now:
a background task's result is never the user's answer.

This holds however the turn started. An orchestrator is woken by teammate or other-session
messages (`<teammate-message>`, "Another Claude session sent a message: <agent-message ...>")
and task notifications, and the user still reads the message that closes that turn: judge it
exactly like a reply to the user.

Block (block=true) when the closing message:
- puts a question or decision to the user in prose: "one decision for you", "holding for your
  pick", "still yours to decide", "your call", "up to you", "say the word", "let me know
  which", "if you'd rather", an A-or-B fork laid out as options, a deferral list parked under a
  heading such as "Still yours to decide:", "the lane recommends X and is proceeding on X" (a
  decision the user never got a real prompt for), or a "want me to ...?" / "should I ...?"
  offer closing the message;
- lists items pending on the user that the user could act on now: a "Waiting on you", "Still
  with you" or "Needs you" section, PRs or designs "held for your read" or "held for your
  word", approvals, reads or clicks the user owes ("your two console clicks"), "it comes to you
  for approval", "I'll bring it to you".

An AskUserQuestion call covers only what it asked. Match each item the closing message leaves
on the user against the questions the transcript shows were asked this turn: an item no call
asked about still counts, however many other questions the turn asked.

Do NOT block when:
- every such item was put to the user with AskUserQuestion or ExitPlanMode this turn and the
  message only reports the answer or the state it left;
- an item cannot be put to the user yet because what the user would act on is still being
  produced (a plan, a diff, a report), and the message names what it waits on and who is
  producing it: "comes to you once the review lane returns the diff";
- the message states a standing rule for a future event rather than a pending item: "any
  create, delete, or replace comes to you";
- the item is something the agent owes the user, or an approval the agent itself grants to
  its lanes; "sent to me" and "I approve" are the agent, not the user;
- the question is rhetorical and answered, quotes or reports someone else's question, or is
  addressed to a subagent, teammate, or tool rather than the user;
- the phrase sits only inside a code block or quoted text the agent is reporting (a hook
  message, a log line, a draft), not in the agent's own words to the user;
- the message reports finished work with nothing left on the user;
- the decision is on a live cc-present board (a `present` skill or `cc-present start` in this
  session) and the prose refers the user to it;
- the only thing left on the user is an action the agent cannot take for them, such as running
  a command (a login, an MFA tap), and the message names the exact command, often in the
  `! <cmd>` form: "Waiting on your SSO login (`! aws sso login`)". That is an action, not a
  choice, and prose naming the command is the right way to ask for it. A choice in the same
  message still counts.

When uncertain, return block=false. Put your reasoning (under 40 words, quoting the prose)
in `reasoning`.""",
    message=(
        "Your closing message leaves something waiting on the user in prose. "
        "Ask it now with `AskUserQuestion` (2-4 options, recommended first), or state what it waits on "
        "and who is producing it."
    ),
    label="narrate_then_wait",
    signals=Signals(
        [
            Signal(pattern=r"(?i)\bone decision for you\b", weight=2),
            Signal(pattern=rf"(?i)\b(?:holding|waiting) (?:for|on) (?:your|you)\b{NO_COMMAND_FOR_YOU}", weight=2),
            Signal(pattern=r"(?i)\b(?:still|now|back) (?:with|on) you\b", weight=2),
            Signal(pattern=r"(?i)\bheld (?:for|on|until) (?:your|you)\b", weight=2),
            Signal(
                pattern=rf"(?i)\b(?:needs?|awaits?|awaiting|requires?) (?:your|you)\b{NO_COMMAND_FOR_YOU}", weight=2
            ),
            Signal(
                pattern=r"(?i)\byour (?:\w+ ){0,2}(?:reads?|reviews?|approvals?|sign-?offs?|clicks?|hand[- ]?steps?)\b",
                weight=2,
            ),
            Signal(pattern=r"(?i)\byour (?:go|go-?ahead|word|nod|ok|okay)\b(?!-to)", weight=2),
            Signal(pattern=r"(?i)\bbring (?:it|them|this|that|these|those) (?:back )?to you\b", weight=2),
            Signal(
                pattern=r"(?i)\b(?:comes?|goes) (?:back )?to you for (?:an? )?(?:approval|review|read|sign-?off)\b",
                weight=2,
            ),
            Signal(pattern=r"(?i)\b(?:comes?|goes) (?:back )?to you\b", weight=1),
            Signal(pattern=r"(?i)\bowe you\b", weight=1),
            Signal(pattern=r"(?i)\b(?:let me know|tell me) (?:which|what|if|whether|how)\b", weight=2),
            Signal(pattern=r"(?i)\byour (?:call|pick|decision|choice|move|shout)\b", weight=2),
            Signal(pattern=r"(?i)\byours? to (?:decide|call|choose)\b", weight=2),
            Signal(pattern=r"(?i)\bup to you\b", weight=2),
            Signal(pattern=r"(?i)\bsay the word\b", weight=2),
            Signal(pattern=r"(?i)\bif you(?:'|’)?d (?:rather|prefer)\b", weight=2),
            Signal(pattern=r"(?i)\bleave (?:it|that|this|them) (?:up )?to you\b", weight=2),
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
        FromSubagent(),
        ContinuingStop(),
        AskToolDisallowed(),
        AskedLast(),
        UsedSkill("present", scope="session", subagents=False),
        RanCommand(Regex(r"^(?:\S*/)?cc-present start\b"), subagents=False),
    ],
    guards_waiting=False,
    once_per_turn=False,
    budget=Budget(turn_chars=8000),
    events=Event.Stop,
    tests={
        Input(transcript=[T.assistant(PROSE_DECISION)]): Block(pattern="AskUserQuestion"),
        Input(transcript=[T.assistant("Holding for your pick: rebase onto dev or cherry-pick the fix?")]): Block(
            pattern="AskUserQuestion"
        ),
        Input(
            transcript=[
                T.assistant(
                    "Both stacks are green and the boot stack is four PRs deep.\n\n"
                    "Still yours to decide at the end: the pool PRs."
                )
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[T.assistant("Still yours to decide: the pool PRs.")],
            background_tasks=[
                {"id": "t1", "type": "subagent", "status": "running", "description": "pr-watcher on the pool PRs"}
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.assistant("Still yours to decide: the pool PRs."),
                *(T.assistant(T.tool("Read", file_path=f"api/src/f{n}.ts")) for n in range(8)),
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[T.assistant("The cap is 30 slots today — up to you whether we raise it or shed jobs.")]
        ): Block(pattern="AskUserQuestion"),
        Input(transcript=[T.user("what is waiting on me?"), T.assistant(STILL_WITH_YOU)]): Block(
            pattern="AskUserQuestion"
        ),
        Input(transcript=[T.user("where is the drive?"), T.assistant(CONSOLE_CLICKS)]): Block(
            pattern="AskUserQuestion"
        ),
        Input(
            transcript=[
                T.user("roll back the release now"),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Roll back the release?"}])),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Prove the stack apply?"}])),
                T.assistant(WAITING_ON_YOU),
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Rebase onto dev?"}])),
                T.assistant("Still yours to decide: the pool PRs."),
                *(T.assistant(T.tool("Read", file_path=f"api/src/f{n}.ts")) for n in range(3)),
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.user("what is the overall status of the deploy cli?"),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Hold the design PR?"}])),
                T.assistant("Holding the design PR as you chose."),
                T.user(DESK_WAKE),
                T.assistant(STILL_WITH_YOU),
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.user("status?"),
                T.assistant("Desk relayed the answers."),
                T.user(DESK_WAKE),
                T.assistant(STILL_WITH_YOU),
            ],
            state=[PrimitiveState(last_fired_at=2)],
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.assistant(PROSE_DECISION),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Keep the firewall rule?"}])),
            ]
        ): Allow(),
        Input(transcript=[T.assistant("The narrow ci plan is ready; it comes to you for approval.")]): Block(
            pattern="AskUserQuestion"
        ),
        Input(transcript=[T.assistant("Applied the fix to your go-to helper; CI is green.")]): Allow(),
        Input(
            agent_id="tm1",
            transcript=[T.assistant("Holding for your pick: rebase onto dev or cherry-pick the fix?")],
        ): Allow(),
        Input(
            disallowed_tools=("AskUserQuestion", "EnterPlanMode", "ExitPlanMode"),
            transcript=[T.assistant("Holding for your pick: rebase onto dev or cherry-pick the fix?")],
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "The gate now tells the session:\n\n```\nHolding for your pick: rebase onto dev or "
                    "cherry-pick the fix?\n```\n\nShipped the fix; CI is green."
                )
            ],
            llm={"block": False},
        ): Allow(),
        Input(transcript=[T.user("status?"), T.assistant(GO_GIVEN)]): Allow(),
        Input(transcript=[T.user("what about api and restate?"), T.assistant(OWED_REPORT)]): Allow(),
        Input(transcript=[T.user(DESK_WAKE), T.assistant(APPROVAL_CONDITIONS)]): Allow(),
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
        Input(transcript=[T.assistant("Shipped the fix; CI is green.")]): Allow(),
        Input(transcript=[T.assistant(SSO_LOGIN)]): Allow(),
        Input(
            transcript=[T.assistant("The S3 proof is blocked.\n\nWaiting on your SSO login:\n```\n! aws login\n```")]
        ): Allow(),
        Input(transcript=[T.assistant("Waiting on your SSO login before the S3 proof can run.")]): Block(
            pattern="AskUserQuestion"
        ),
        Input(
            transcript=[T.assistant(f"{SSO_LOGIN}\n\nAlso your call: rebase onto dev or cherry-pick the fix?")]
        ): Block(pattern="AskUserQuestion"),
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
