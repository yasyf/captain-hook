from __future__ import annotations

import json

from spawnllm import Binary, BinaryAnswer

from captain_hook import (
    Allow,
    BaseHookEvent,
    Event,
    HookResult,
    Input,
    Signal,
    Signals,
    T,
    Tool,
    UserMessages,
    Warn,
    on,
)
from captain_hook.primitives.llm import consume_signals
from captain_hook.state import fired_this_turn, record_fire

DETOUR_QUESTIONS = {
    "side_work": Binary(
        "Is the agent's current call side work that user_messages never asked for and that the requested task does not "
        "need?",
        yes="The call works on something adjacent that nobody asked for.",
        no="The call is the requested task, something it needs, or something user_messages allowed.",
    ),
    "authorized": Binary(
        "Do user_messages allow this kind of extra work, for example with 'also', 'while you're there', or 'fix "
        "anything you find'?"
    ),
    "prerequisite": Binary(
        "Does the requested task need the current call to land or be verified, for example a failing build or test "
        "the task trips over?"
    ),
    "surfaced": Binary(
        "Did the agent already put the side work to the user or its orchestrator as options instead of acting on it?"
    ),
    "gathering": Binary(
        "Is the current call only reading or gathering information rather than changing files, state, or anything "
        "outside?"
    ),
}
SIDE_WORK = 0.8
EXEMPT = 0.5
DETOUR_MESSAGE = (
    "This looks like a detour: side work nobody asked for. Stop and ask via `AskUserQuestion` with 2-4 options "
    "(file a follow-up, fix it now, or ignore it); a delegated agent returns early with findings plus options."
)
YES = BinaryAnswer(p_yes=0.97, confidence=0.94)
NO = BinaryAnswer(p_yes=0.03, confidence=0.94)
DETOUR = {"side_work": YES, "authorized": NO, "prerequisite": NO, "surfaced": NO, "gathering": NO}
ON_TASK = DETOUR | {"side_work": NO}
DETOUR_SIGNALS = Signals(
    [
        Signal(pattern=r"(?i)\bwhile (?:I'm|I am|we're|we are) (?:here|at it|in (?:here|there))\b", weight=2),
        Signal(pattern=r"(?i)\bmight as well\b", weight=2),
        Signal(pattern=r"(?i)\b(?:let me|I'll|I will) also\b", weight=2),
        Signal(pattern=r"(?i)\bas a bonus\b", weight=2),
        Signal(pattern=r"(?i)\bI (?:also )?noticed\b", weight=1),
        Signal(pattern=r"(?i)\b(?:unrelated|a side note|tangent)\b", weight=1),
        Signal(pattern=r"(?i)\bone more thing\b", weight=1),
        Signal(pattern=r"(?i)\bquick(?:ly)? (?:fix|clean|tidy|refactor)\w*\b", weight=1),
    ],
    threshold=2,
    window=8,
    scope="window",
)


def detour_state(evt: BaseHookEvent, asked: str) -> dict[str, str]:
    raw = dict(evt.input.raw)
    call = (
        raw.get("command")
        if evt.tool_name == "Bash"
        else json.dumps({key: str(value)[:800] for key, value in raw.items()})
    )
    return {
        "user_messages": asked,
        "recent_transcript": evt.ctx.transcript_text(window=10)[-3000:],
        "current_call": f"{evt.tool_name}: {str(call or '')[:1500]}",
    }


@on(
    Event.PostToolUse,
    only_if=[Tool("Edit|Write|MultiEdit|NotebookEdit|Bash")],
    max_fires=3,
    tests={
        Input(
            decide=DETOUR,
            file="client.py",
            content="retry = 3\n",
            transcript=[
                T.user("Rename the config flag in settings.py."),
                T.assistant("While I'm here, the retry logic looks wrong — fixing it too."),
            ],
        ): Warn(pattern="detour"),
        Input(
            decide=DETOUR,
            command="./scripts/cleanup.sh",
            transcript=[
                T.user("Add retries to the fetch client."),
                T.assistant("I also noticed stale artifacts. One more thing to clean up."),
            ],
        ): Warn(pattern="options"),
        Input(
            file="flags.py",
            content="json_flag = True\n",
            transcript=[
                T.user("Add a --json flag to the CLI."),
                T.assistant("Implementing the requested --json flag now."),
            ],
        ): Allow(),
        Input(
            tool="Read",
            file="client.py",
            transcript=[
                T.user("Rename the config flag."),
                T.assistant("While I'm here, might as well look at the retry logic."),
            ],
        ): Allow(),
        Input(
            file="backends/store.go",
            content="client = ccnotes.New()\n",
            decide=ON_TASK,
            transcript=[
                T.user(
                    "Audit the changelog. Also migrate all our backends to the new ccnotes "
                    "interface and clean up anything fleet-outdated while you're at it."
                ),
                *(T.assistant(f"Edited backends/store_{i}.go per the migration plan.") for i in range(18)),
                T.assistant("Let me also migrate the last backend, then a quick cleanup."),
            ],
        ): Allow(),
        Input(
            decide=DETOUR,
            file="client.py",
            content="retry = 3\n",
            transcript=[
                T.user("Rename the config flag in settings.py."),
                *(T.assistant(f"Edited backends/store_{i}.go per the migration plan.") for i in range(18)),
                T.assistant("Let me also fix the retry logic while I'm at it — quick fix."),
            ],
        ): Warn(pattern="detour"),
        Input(
            file="client.py",
            content="retry = 3\n",
            decide=DETOUR | {"prerequisite": YES},
            transcript=[
                T.user("Fix the failing test in settings.py."),
                T.assistant("While I'm here, the retry helper the test calls is broken — fixing it first."),
            ],
        ): Allow(),
        Input(
            file="client.py",
            content="retry = 3\n",
            decide={"error": TimeoutError()},
            transcript=[
                T.user("Rename the config flag in settings.py."),
                T.assistant("While I'm here, the retry logic looks wrong — fixing it too."),
            ],
        ): Allow(),
        Input(
            agent_id="tm1",
            command="./scripts/fix-flaky-tests.sh",
            decide=DETOUR,
            transcript=[
                T.user(
                    '<teammate-message teammate_id="team-lead">Watch PRs 17371 and 17372 until '
                    "both are merged, then report back.</teammate-message>",
                    isSidechain=True,
                ),
                T.assistant(
                    "Let me also rewrite the flaky test I noticed while I'm at it.",
                    isSidechain=True,
                ),
            ],
        ): Warn(pattern="detour"),
    },
)
def detours(evt: BaseHookEvent) -> HookResult | None:
    if fired_this_turn(evt) or consume_signals(evt, DETOUR_SIGNALS, "detours") is None:
        return None
    if (asked := UserMessages().content(evt)) is None:
        return None
    match (decision := evt.decide(detour_state(evt, asked), DETOUR_QUESTIONS)) and decision.answers:
        case {"side_work": BinaryAnswer(p_yes=side_work), **exemptions} if side_work >= SIDE_WORK and all(
            isinstance(answer, BinaryAnswer) and answer.p_yes < EXEMPT for answer in exemptions.values()
        ):
            record_fire(evt)
            return evt.warn(DETOUR_MESSAGE)
        case _:
            return None
