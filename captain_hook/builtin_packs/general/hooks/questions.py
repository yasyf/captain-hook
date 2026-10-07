from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cc_transcript.models import AssistantEvent, UserEvent, tool_uses
from spawnllm import Binary, BinaryAnswer, Refused

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    CustomCondition,
    Event,
    FromSubagent,
    HookResult,
    Input,
    RanCommand,
    Regex,
    Signal,
    Signals,
    T,
    UsedSkill,
    on,
)
from captain_hook.primitives.llm import consume_signals
from captain_hook.signals import matching_signals
from captain_hook.state import PrimitiveState, record_fire

if TYPE_CHECKING:
    from collections.abc import Sequence

ASK_TOOLS = "AskUserQuestion|ExitPlanMode"
SENTENCE_REST = r"(?:(?!\n\n)[^.])*?"
NO_COMMAND_FOR_YOU = (
    rf"(?!(?={SENTENCE_REST}[`\n]!\s)"
    rf"(?!{SENTENCE_REST}\b(?:choose|choice|pick|decide|either|whether|which|or|rather|prefer)\b))"
)

WAIT_LEAD = re.compile(r"(?i)^(?:also\s+)?(?:still\s+)?waiting on(?:\s+(?:\w+\s+)?things?)?\s*:?\s*")
PRODUCER = (
    r",\s+(?:which|that)\s+(?:only\s+)?[^,;.]+?(?:\s+(?:is|are)|['’](?:s|re))\s+(?:still\s+)?\w+ing\b"
    r"|,\s+(?:which|that)\s+only\s+[^,;.]+?\s+can\s+\w+"
)
PRODUCED_ITEM = rf"(?:(?!\s(?:and|or)\s)[^,;])+?(?:{PRODUCER})"
PRODUCED_ITEMS = re.compile(rf"(?i){PRODUCED_ITEM}(?:(?:,?\s+and\s+|;\s+|,\s+){PRODUCED_ITEM})*\.?")
DECISION = re.compile(
    r"(?i)\b(?:picks?|picking|choices?|choos(?:e|es|ing)|decid(?:e|es|ing)|decisions?|approv(?:e|es|ing|als?)"
    r"|confirm(?:s|ing|ations?)?|rulings?|rul(?:e|es|ing)\s+on|answers?|answering|sign(?:s|ing)?[- ]?offs?"
    r"|mak(?:e|es|ing)\s+(?:a\s+|the\s+)?call|your\s+call)\b"
)
BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")

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

NAMED_WAIT = "Waiting on: the owner's review verdict for the memory stack, which the owner is producing."

NAMED_WAITS = (
    "The urgent-message watch ran its full 30 minutes with nothing to report, so I re-armed it.\n"
    "\n"
    "Waiting on two things:\n"
    "- The `/live-dashboard` skill, which workflow `wf_12526d50-6b2` is still building.\n"
    "- Your review verdict on the memory stack, which only you can give."
)

DESK_WAKE = (
    'Another Claude session sent a message: <agent-message from="landing-desk"> Two PRs landed '
    "as squashes on dev. </agent-message>"
)


NARRATE_SIGNALS = Signals(
    [
        Signal(pattern=r"(?i)\bone decision for you\b", weight=2),
        Signal(pattern=rf"(?i)\b(?:holding|waiting) (?:for|on) (?:your|you)\b{NO_COMMAND_FOR_YOU}", weight=2),
        Signal(pattern=r"(?i)\b(?:still|now|back) (?:with|on) you\b", weight=2),
        Signal(pattern=r"(?i)\bheld (?:for|on|until) (?:your|you)\b", weight=2),
        Signal(pattern=rf"(?i)\b(?:needs?|awaits?|awaiting|requires?) (?:your|you)\b{NO_COMMAND_FOR_YOU}", weight=2),
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
)


def produced(waits: str) -> bool:
    return PRODUCED_ITEMS.fullmatch(waits) is not None and DECISION.search(waits) is None


def undeclared_prose(closing: str) -> str | None:
    rest: list[str] = []
    declared = awaiting = listing = False
    for line in closing.splitlines():
        if (awaiting or listing) and (bullet := BULLET.match(line)):
            if not produced(line[bullet.end() :].strip()):
                return None
            awaiting, listing = False, True
            continue
        if not line.strip():
            continue
        if awaiting:
            return None
        listing = False
        for sentence in SENTENCE_BREAK.split(line):
            if (lead := WAIT_LEAD.match(sentence)) is None:
                rest.append(sentence)
            elif waits := sentence[lead.end() :].strip():
                if not produced(waits):
                    return None
                declared = True
            else:
                declared = awaiting = True
    return None if awaiting or not declared else "\n".join(rest)


class AskedLast(CustomCondition):
    """True when the turn's last act was an ask tool call with no prose after it."""

    def check(self, evt: BaseHookEvent) -> bool:
        since = evt.ctx.t.after(tool=ASK_TOOLS)
        return evt.ctx.t.has_tool(ASK_TOOLS, subagents=False) and not (
            (count := len(since)) and since.assistant_text(count, max_per_msg=1)
        )


class WaitsNamed(CustomCondition):
    """True when the closing message's waits each name their producer and nothing else in it trips the gate."""

    def check(self, evt: BaseHookEvent) -> bool:
        rest = undeclared_prose(evt.ctx.t.assistant_text(1, max_per_msg=20000))
        return rest is not None and not matching_signals(NARRATE_SIGNALS.patterns, rest)


class ContinuingStop(CustomCondition):
    """True when this Stop continues a turn an earlier Stop block already extended."""

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.stop_hook_active


class AskToolDisallowed(CustomCondition):
    """True when the session was launched without the `AskUserQuestion` tool."""

    def check(self, evt: BaseHookEvent) -> bool:
        return "AskUserQuestion" in evt.disallowed_tools


NARRATE_QUESTIONS = {
    "leaves_on_user": Binary(
        "Does the closing message leave something pending on the user, the human reading it, that the user could act "
        "on now: a decision or choice, a question to answer, an approval, a read or review of a PR, design, or list, a "
        "click or hand step, or a go-ahead?",
        yes="Something waits on the user's decision, answer, approval, read, click, or go-ahead.",
        no="Nothing waits on the user; the message only reports work, plans, or what others are doing.",
    ),
    "still_produced": Binary(
        "Is everything waiting on the user something that is still being produced by someone else, such as a lane, a "
        "review, CI, or a report, with the closing message naming what it waits on?",
        yes="The user cannot act yet; the message names the work still being produced and who produces it.",
        no="The user could act on at least one item now.",
    ),
    "asked_covered": Binary(
        "Is every item the closing message leaves on the user among the questions listed in asked_with_tool_this_turn?"
    ),
}
LEAVES_ON_USER = 0.65
EXEMPT = 0.45
NARRATE_MESSAGE = (
    "Your closing message leaves something waiting on the user in prose. "
    "Ask it now with `AskUserQuestion` (2-4 options, recommended first), or state what it waits on "
    "and who is producing it."
)
YES = BinaryAnswer(p_yes=0.97, confidence=0.94)
NO = BinaryAnswer(p_yes=0.03, confidence=0.94)
BLOCKING = {"leaves_on_user": YES, "still_produced": NO, "asked_covered": NO}
CLEAR = {"leaves_on_user": NO, "still_produced": NO, "asked_covered": NO}


def asked_this_turn(events: Sequence[object]) -> list[str]:
    asked: list[str] = []
    for use in (use for event in events if isinstance(event, AssistantEvent) for use in tool_uses(event)):
        if use.name == "AskUserQuestion":
            asked.extend(question.get("question", "") for question in use.input.get("questions") or ())
        elif use.name == "ExitPlanMode":
            asked.append("a plan submitted for approval with ExitPlanMode")
    return asked


def narration(evt: BaseHookEvent) -> dict[str, object]:
    events = evt.ctx.t.current_turn.events
    opener = next(
        (
            event.text
            for event in reversed(events)
            if isinstance(event, UserEvent) and event.text and not event.text.startswith("Stop hook feedback:")
        ),
        "",
    )
    return {
        "turn_opener": opener[-2000:],
        "asked_with_tool_this_turn": asked_this_turn(events),
        "closing_message": evt.ctx.t.assistant_text(1, max_per_msg=20000)[-6000:],
    }


@on(
    Event.Stop,
    skip_if=[
        FromSubagent(),
        ContinuingStop(),
        AskToolDisallowed(),
        AskedLast(),
        WaitsNamed(),
        UsedSkill("present", scope="session", subagents=False),
        RanCommand(Regex(r"^(?:\S*/)?cc-present start\b"), subagents=False),
    ],
    tests={
        Input(decide=BLOCKING, transcript=[T.assistant(PROSE_DECISION)]): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING, transcript=[T.assistant("Holding for your pick: rebase onto dev or cherry-pick the fix?")]
        ): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING,
            transcript=[
                T.assistant(
                    "Both stacks are green and the boot stack is four PRs deep.\n\n"
                    "Still yours to decide at the end: the pool PRs."
                )
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING,
            transcript=[T.assistant("Still yours to decide: the pool PRs.")],
            background_tasks=[
                {"id": "t1", "type": "subagent", "status": "running", "description": "pr-watcher on the pool PRs"}
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING,
            transcript=[
                T.assistant("Still yours to decide: the pool PRs."),
                *(T.assistant(T.tool("Read", file_path=f"api/src/f{n}.ts")) for n in range(8)),
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING,
            transcript=[T.assistant("The cap is 30 slots today — up to you whether we raise it or shed jobs.")],
        ): Block(pattern="AskUserQuestion"),
        Input(decide=BLOCKING, transcript=[T.user("what is waiting on me?"), T.assistant(STILL_WITH_YOU)]): Block(
            pattern="AskUserQuestion"
        ),
        Input(decide=BLOCKING, transcript=[T.user("where is the drive?"), T.assistant(CONSOLE_CLICKS)]): Block(
            pattern="AskUserQuestion"
        ),
        Input(
            decide=BLOCKING,
            transcript=[
                T.user("roll back the release now"),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Roll back the release?"}])),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Prove the stack apply?"}])),
                T.assistant(WAITING_ON_YOU),
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING,
            transcript=[
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Rebase onto dev?"}])),
                T.assistant("Still yours to decide: the pool PRs."),
                *(T.assistant(T.tool("Read", file_path=f"api/src/f{n}.ts")) for n in range(3)),
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING,
            transcript=[
                T.user("what is the overall status of the deploy cli?"),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Hold the design PR?"}])),
                T.assistant("Holding the design PR as you chose."),
                T.user(DESK_WAKE),
                T.assistant(STILL_WITH_YOU),
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING,
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
        Input(
            decide=BLOCKING, transcript=[T.assistant("The narrow ci plan is ready; it comes to you for approval.")]
        ): Block(pattern="AskUserQuestion"),
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
            decide=CLEAR,
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
            decide=BLOCKING,
            transcript=[
                T.assistant("Waiting on you to choose staging or production before running `! aws sso login`.")
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[T.assistant("The S3 proof is blocked.\n\nWaiting on your SSO login:\n```\n! aws login\n```")]
        ): Allow(),
        Input(
            decide=BLOCKING, transcript=[T.assistant("Waiting on your SSO login before the S3 proof can run.")]
        ): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING,
            transcript=[T.assistant(f"{SSO_LOGIN}\n\nAlso your call: rebase onto dev or cherry-pick the fix?")],
        ): Block(pattern="AskUserQuestion"),
        Input(transcript=[T.user(DESK_WAKE), T.assistant(NAMED_WAIT)]): Allow(),
        Input(transcript=[T.assistant("Holding for your pick on the pool PRs."), T.assistant(NAMED_WAITS)]): Allow(),
        Input(decide=BLOCKING, transcript=[T.assistant("Let me know which.")]): Block(pattern="AskUserQuestion"),
        Input(decide=BLOCKING, transcript=[T.assistant("Waiting on your pick, which only you can make.")]): Block(
            pattern="AskUserQuestion"
        ),
        Input(
            decide=BLOCKING,
            transcript=[
                T.assistant("Waiting on your release plan, which only you can approve.\nThe watch is re-armed.")
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(transcript=[T.assistant(f"{NAMED_WAIT}\nThe watch is re-armed.")]): Allow(),
        Input(decide=BLOCKING, transcript=[T.assistant(f"{NAMED_WAIT} Let me know which.")]): Block(
            pattern="AskUserQuestion"
        ),
        Input(
            decide=BLOCKING,
            transcript=[
                T.assistant(
                    "Waiting on your review verdict, which the owner is producing.\nCan you approve the deployment?"
                )
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING,
            transcript=[T.assistant("Waiting on your approval and the CI report, which the CI lane is producing.")],
        ): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING,
            transcript=[
                T.assistant("Waiting on two things:\n- The diff, which the review lane is producing.\n- Your pick.")
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            decide=BLOCKING | {"still_produced": YES},
            transcript=[T.assistant("The narrow ci plan comes to you for approval once the plan lane returns it.")],
        ): Allow(),
        Input(
            decide=BLOCKING | {"asked_covered": YES},
            transcript=[T.assistant("Holding for your pick: rebase onto dev or cherry-pick the fix?")],
        ): Allow(),
        Input(
            decide=BLOCKING | {"leaves_on_user": Refused()},
            transcript=[T.assistant("Holding for your pick: rebase onto dev or cherry-pick the fix?")],
        ): Allow(),
        Input(
            decide={"error": TimeoutError()},
            transcript=[T.assistant("Holding for your pick: rebase onto dev or cherry-pick the fix?")],
        ): Allow(),
        Input(
            transcript=[T.assistant("Both pool PRs are merged; nothing is left to decide.")],
            decide=CLEAR,
        ): Allow(),
        Input(
            transcript=[T.assistant("Why did the build fail? Let me know which key went stale: the lockfile.")],
            decide=CLEAR,
        ): Allow(),
    },
)
def narrate_then_wait(evt: BaseHookEvent) -> HookResult | None:
    if consume_signals(evt, NARRATE_SIGNALS, "narrate_then_wait") is None:
        return None
    match (decision := evt.decide(narration(evt), NARRATE_QUESTIONS)) and decision.answers:
        case {
            "leaves_on_user": BinaryAnswer(p_yes=leaves),
            "still_produced": BinaryAnswer(p_yes=produced),
            "asked_covered": BinaryAnswer(p_yes=covered),
        } if leaves >= LEAVES_ON_USER and max(produced, covered) < EXEMPT:
            record_fire(evt)
            return evt.block(NARRATE_MESSAGE)
        case _:
            return None
