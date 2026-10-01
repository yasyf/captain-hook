from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from captain_hook import (
    Allow,
    Block,
    Event,
    FromSubagent,
    HookResult,
    InlineTests,
    Input,
    OtherCall,
    PostToolUseFailureEvent,
    SkillCall,
    T,
    TaskCall,
    Tool,
    Warn,
    WorkflowState,
    llm_nudge,
    on,
    workflow_state,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from captain_hook import BaseHookEvent

TOOLING = frozenset({"ccx", "gt", "orca", "ccn", "cc-notes", "cc-slack", "codex-ask", "capt-hook"})
TOOL_WORD = re.compile(rf"(?<![\w-])({'|'.join(sorted(map(re.escape, TOOLING)))})(?![\w-])")
RAW_FALLBACK = re.compile(r"#\s*ccx:raw\b")
MANUAL = re.compile(r"(?i)\b(?:work[-\s]?around|by hand|manually)\b")
REFUSAL_LINE = re.compile(r"(?i)\b(?:usage|refus\w*|unknown (?:command|flag|option)|unrecognized)\b")
LANE_REPORT = re.compile(r"(?i)\bccx refusal:|\btooling defect\b")
EXIT_HEADER = re.compile(r"\A(?:Error: )?Exit code \d+\n")
REPEATS = 3
SCOPE = "tooling_nudge"
EVIDENCE_CHARS = 240
CC_SLACK_ACTION = re.compile(r"(?i)\bcc-slack\b|\bslack_(?:send|reply|edit|react|watch|dm_status)\b")
GITHUB_ACTION = re.compile(r"(?i)\bccx vcs\b|\bgh (?:pr|api|run)\b|\bstack-enqueue\b")
CC_SLACK_COMMANDS = frozenset({"cc-slack"})
CC_SLACK_MCP = "mcp__plugin_cc-slack_"
LANE_MARKER = re.compile(r"\btooling-lane:\s*(?P<key>[\w:.-]+)")

PROMPT = """You are a senior engineer watching another engineer ("the agent") mid-task. A
deterministic scan flagged the tool call that just ran as a possible TOOLING DEFECT or
TOOL-SHAPED GAP; `<tooling_signal>` names the signal, the tool, and the evidence. Your one
job: decide whether this is a real shortcoming in a first-party tool the agent should get
fixed or built now, instead of working around it.

First-party tools (ccx, gt wrappers, orca, cc-notes/ccn, cc-slack, codex-ask, capt-hook) live
as sibling repos with routine tag-driven releases. The owner's standing rule: a tooling
defect, a refusal, a `# ccx:raw` fallback, or a repeated chore that could be a verb gets a
tooling lane in the same turn, which fixes or builds the tool and ships it (PR, merge,
release, install) while the agent keeps going. Nobody waits to be told.

Fire (fire=true) when the evidence shows the tool itself fell short:
- A first-party tool refused a legitimate request, crashed, or lacks a verb, flag, or
  output the agent needed.
- The agent fell back to a raw command (`# ccx:raw`) or did a step "by hand" because the
  tool could not.
- A papercut or lane report records friction in a tool and nothing is fixing it.
- The same command is being re-run by hand as a loop or poll that a single verb or a
  `--watch`/`--wait` mode would replace.

Do NOT fire when:
- The failure is the agent's own mistake (a typo, a wrong path, a flag it misspelled) and
  the tool's error message told it exactly what to do.
- The tool refused correctly: a safety guard doing its job, a conflict that needs a human,
  an auth or network failure outside the tool.
- The transcript shows a tooling lane for this tool already spawned, the agent is already
  working in that tool's own repo on this fix, or the agent recorded a papercut because it
  cannot spawn lanes.
- The repetition is legitimate distinct work (re-running tests after each edit, reading a
  file again after changing it).

<examples>
<example fire="true">
`ccx vcs stack submit` exits 1 with "ccx: refusing to submit: submodule pointer changed";
the agent's next step is `gt submit --no-interactive  # ccx:raw`.
A ccx gap the agent is routing around; the fix belongs in ccx.
</example>
<example fire="true">
`orca terminal send` run three times with the same text because the worker never received
it.
Message delivery is broken; a tooling lane should fix Orca delivery rather than retrying.
</example>
<example fire="false">
`ccx code read src/mian.py` exits 1 with "ccx: path not found: src/mian.py".
A typo; the tool answered correctly.
</example>
<example fire="false">
`gt submit` exits 1 with "ERROR: merge conflict in api/src/db.ts".
The tool is right; the conflict needs resolving, not a tool fix.
</example>
</examples>

When uncertain, return fire=false. Put your reasoning (under 40 words, naming the tool and
what it should do instead) in `reasoning`."""

MESSAGE = (
    "Tooling defect or tool-shaped gap detected. {reasoning} "
    "Spawn a tooling lane now (opus at high; `lane-ship` in a long-running drive) to fix the tool: "
    "PR, merge, release, install. Do not work around it and do not wait to be told; keep going on "
    "your task meanwhile. If you cannot spawn agents, record a `ccn papercut` naming the defect. "
    "See CLAUDE.md § Tooling (owner rule, 2026-10-01)."
)


@dataclass(frozen=True, slots=True)
class Signature:
    key: str
    tool: str
    text: re.Pattern[str]
    action: re.Pattern[str]
    commands: frozenset[str]
    mcp_prefix: str | None = None

    def sourced(self, evt: BaseHookEvent) -> bool:
        match evt.input:
            case TaskCall():
                return True
            case OtherCall(name=name) if self.mcp_prefix:
                return name.startswith(self.mcp_prefix)
        return bool(evt.command) and any(Path(call.name).name in self.commands for call in evt.command.calls())


SIGNATURES = (
    Signature(
        "cc-slack-session",
        "cc-slack",
        re.compile(r"no cc-slack session for this Claude window"),
        CC_SLACK_ACTION,
        CC_SLACK_COMMANDS,
        CC_SLACK_MCP,
    ),
    Signature(
        "cc-slack-no-watch",
        "cc-slack",
        re.compile(r"\bpass `?no_watch\b"),
        CC_SLACK_ACTION,
        CC_SLACK_COMMANDS,
        CC_SLACK_MCP,
    ),
    Signature(
        "github-quota",
        "ccx",
        re.compile(
            r"(?i)rate-limited until|api rate limit exceeded|secondary rate limit"
            r"|graphql\b[^\n]{0,60}\b(?:quota|rate limit)"
        ),
        GITHUB_ACTION,
        frozenset({"ccx", "gh", "stack-enqueue"}),
    ),
)


class Refusal(BaseModel):
    tool: str
    action: str
    evidence: str
    lane: bool = False


@workflow_state("tooling_refusals")
class ToolingRefusals(WorkflowState):
    refusals: dict[str, Refusal] = {}


REFUSAL_MESSAGE = (
    "{tool} refused: {evidence}\n"
    "Spawn a tooling lane now that fixes {tool} (PR, merge, release, install), with the line "
    "`tooling-lane: {key}` in its prompt, and keep going on your task meanwhile. Until that lane exists, "
    "an Agent or Skill dispatch that repeats this action is blocked. If you cannot spawn agents, report "
    "the refusal verbatim to your orchestrator."
)
REPEAT_MESSAGE = (
    "This dispatch repeats an action {tool} already refused ({evidence}), and no tooling lane for that "
    "refusal exists. Spawn the tooling lane first, with the line `tooling-lane: {key}` in its prompt, then "
    "retry this dispatch."
)


def strings(value: object) -> Iterator[str]:
    match value:
        case str():
            yield value
        case dict():
            for item in value.values():
                yield from strings(item)
        case list() | tuple():
            for item in value:
                yield from strings(item)


def result_text(evt: BaseHookEvent) -> str:
    if isinstance(evt, PostToolUseFailureEvent):
        return evt.error
    return "\n".join(strings(getattr(evt, "tool_response", None)))


def excerpt(text: str, match: re.Match[str]) -> str:
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    return text[start : end if end >= 0 else None].strip()[:EVIDENCE_CHARS]


def refusal(evt: BaseHookEvent) -> tuple[str, Refusal] | None:
    text = result_text(evt)
    for signature in SIGNATURES:
        if signature.sourced(evt) and (found := signature.text.search(text)):
            return signature.key, Refusal(
                tool=signature.tool, action=signature.action.pattern, evidence=excerpt(text, found)
            )
    if evt.command and RAW_FALLBACK.search(raw := evt.command.raw) and (calls := evt.command.calls()):
        verb = " ".join([calls[0].name, *calls[0].args[:1]])
        evidence = f"the step ran raw instead: `{raw[:EVIDENCE_CHARS]}`"
        return f"ccx-raw:{'-'.join(verb.split())}", Refusal(tool="ccx", action=re.escape(verb), evidence=evidence)
    return None


def dispatch_text(evt: BaseHookEvent) -> str:
    match evt.input:
        case TaskCall(prompt=prompt):
            return prompt
        case SkillCall(skill=skill, args=args):
            return f"{skill} {args or ''}"
    return ""


@dataclass(frozen=True, slots=True)
class Detection:
    signal: str
    tool: str
    evidence: str

    @property
    def key(self) -> str:
        return f"{self.signal}:{self.tool}"


def first_output_line(text: str) -> str:
    return next((line.strip() for line in EXIT_HEADER.sub("", text).splitlines() if line.strip()), "")


def repeated_command(evt: BaseHookEvent, raw: str) -> bool:
    exact = re.compile("^" + re.escape(raw).replace("\\ ", " ") + "$")
    calls = evt.ctx.t.tool_calls.named("Bash").where_input(command=exact)
    return calls.count() + calls.failed().count() >= REPEATS


def detect_bash(evt: BaseHookEvent) -> Detection | None:
    raw = evt.command.raw
    calls = evt.command.calls()
    head = calls[0].name if calls else ""
    if any(call.name in {"ccn", "cc-notes"} and call.args[:1] == ("papercut",) for call in calls):
        return Detection("papercut recorded", "cc-notes", raw)
    if isinstance(evt, PostToolUseFailureEvent):
        line = first_output_line(evt.error)
        for call in calls:
            if call.name in TOOLING and (call.name in line or REFUSAL_LINE.search(line)):
                return Detection(f"{call.name} refusal or usage error", call.name, f"$ {raw}\n{line}")
    if MANUAL.search(raw) and (word := TOOL_WORD.search(raw)):
        return Detection("manual workaround next to a tool", word.group(1), raw)
    if raw and repeated_command(evt, raw):
        return Detection(f"same command run {REPEATS}+ times", head, raw)
    return None


def detect(evt: BaseHookEvent) -> Detection | None:
    match evt.input:
        case OtherCall(name="SendMessage", raw=raw) if LANE_REPORT.search(text := str(raw.get("message", ""))):
            word = TOOL_WORD.search(text)
            return Detection("lane report names a tooling defect", word.group(1) if word else "lane", text)
        case TaskCall() if LANE_REPORT.search(text := str(getattr(evt, "tool_response", None) or "")):
            word = TOOL_WORD.search(text)
            return Detection("lane report names a tooling defect", word.group(1) if word else "lane", text)
        case OtherCall(name=name) if name.endswith("papercut"):
            return Detection("papercut recorded", "cc-notes", str(evt.input.raw))
    return detect_bash(evt) if evt.command else None


def claim(evt: BaseHookEvent) -> bool:
    return refusal(evt) is None and (found := detect(evt)) is not None and evt.ctx.s.once(found.key, scope=SCOPE)


@dataclass(frozen=True, slots=True)
class ToolingSignal:
    """Gating context: the deterministic tooling signal this tool call tripped, with its evidence."""

    tag: str = "tooling_signal"
    required: bool = True

    def content(self, evt: BaseHookEvent) -> str | None:
        if (found := detect(evt)) is None:
            return None
        return f"signal: {found.signal}\ntool: {found.tool}\nevidence:\n{found.evidence[:1500]}"


def tooling_nudge(events: Event, tools: tuple[str, ...], tests: InlineTests) -> None:
    llm_nudge(
        PROMPT,
        label="tooling_nudge",
        message=MESSAGE,
        only_if=[Tool(*tools)],
        events=events,
        when=claim,
        max_fires=None,
        contexts=[ToolingSignal()],
        agent=False,
        transcript=True,
        tests=tests,
    )


def bash_calls(command: str, n: int, *, is_error: bool = False) -> list[dict[str, object]]:
    return [line for _ in range(n) for line in T.tool_turn("Bash", command=command, is_error=is_error)]


tooling_nudge(
    Event.PostToolUseFailure,
    ("Bash",),
    {
        Input(
            command="ccx vcs stack submit",
            error="Exit code 1\nccx: refusing to submit: submodule pointer changed",
        ): Warn(pattern="Spawn a tooling lane"),
        Input(command="gt sync", error="Exit code 2\nUsage: gt sync [options]"): Warn(pattern="tooling lane"),
        Input(
            command="gt track --parent dev  # ccx:raw",
            error="Exit code 1\nERROR: branch already tracked",
        ): Allow(),
        Input(
            command="ccx code read src/mian.py",
            error="Exit code 1\nccx: path not found: src/mian.py",
            llm={"fire": False},
        ): Allow(),
        Input(command="pytest -q", error="Exit code 1\nFAILED tests/test_x.py::test_y"): Allow(),
        Input(
            command="ccx vcs stack submit",
            error="Exit code 1\nccx: refusing to submit: submodule pointer changed",
            seen={SCOPE: ["ccx refusal or usage error:ccx"]},
        ): Allow(),
    },
)

tooling_nudge(
    Event.PostToolUse,
    ("Bash", "SendMessage", "Agent", "Task", "papercut"),
    {
        Input(command="gh pr edit 123 --base dev  # ccx:raw"): Allow(),
        Input(command="ccn papercut 'ccx ship drops the PR body'"): Warn(pattern="ccn papercut"),
        Input(
            tool="mcp__plugin_cc-notes_cc-notes__papercut",
            tool_input={"title": "orca send never delivers"},
        ): Warn(pattern="tooling lane"),
        Input(command="gh pr edit 9 --base dev  # retarget manually since ccx refuses"): Warn(pattern="tooling lane"),
        Input(
            command="orca terminal send w1 'status?'",
            transcript=bash_calls("orca terminal send w1 'status?'", 3),
        ): Warn(pattern="tooling lane"),
        Input(
            tool="SendMessage",
            tool_input={"to": "team-lead", "message": "Done. ccx refusal: stack submit rejects submodules."},
        ): Warn(pattern="tooling lane"),
        Input(
            tool="Agent",
            tool_input={"prompt": "land the stack", "subagent_type": "lane"},
            output="Landed. tooling defect: gt restack replays merged commits.",
        ): Warn(pattern="tooling lane"),
        Input(
            command="ccx vcs pr status 12",
            transcript=bash_calls("ccx vcs pr status 12", 3, is_error=True),
        ): Warn(pattern="tooling lane"),
        Input(
            command="orca terminal send w1 'status?'",
            transcript=bash_calls("orca terminal send w1 'status?'", 2),
        ): Allow(),
        Input(command="git status"): Allow(),
        Input(command="ccx vcs status"): Allow(),
        Input(tool="SendMessage", tool_input={"to": "team-lead", "message": "PR #12 merged."}): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "reply in the thread", "subagent_type": "lane"},
            output="Blocked: no cc-slack session for this Claude window.",
        ): Allow(),
    },
)


@on(
    Event.PostToolUse | Event.PostToolUseFailure,
    tests={
        Input(
            tool="mcp__plugin_cc-slack_cc-slack__slack_reply",
            tool_input={"channel_id": "C1", "thread_ts": "1.2", "text": "hi"},
            output="no cc-slack session for this Claude window; run cc-slack login",
        ): Warn(pattern=r"^cc-slack refused: no cc-slack session.*\n.*`tooling-lane: cc-slack-session`"),
        Input(
            tool="Agent",
            tool_input={"prompt": "reply in the thread", "subagent_type": "lane"},
            output="Could not post: cc-slack says to pass no_watch when the thread is already watched.",
        ): Warn(pattern=r"`tooling-lane: cc-slack-no-watch`"),
        Input(
            command="ccx vcs pr status 12",
            output="ccx: GitHub GraphQL quota exhausted; rate-limited until 14:05",
        ): Warn(pattern=r"^ccx refused: ccx: GitHub GraphQL quota exhausted"),
        Input(command="gh pr edit 123 --base dev  # ccx:raw"): Warn(pattern=r"`tooling-lane: ccx-raw:gh-pr`"),
        Input(
            command="gh pr edit 123 --base dev  # ccx:raw",
            agent_id="a1b2c3",
            seen={SCOPE: ["ccx-raw:gh-pr:main"]},
        ): Warn(pattern="report the refusal verbatim"),
        Input(command="gh pr edit 123 --base dev  # ccx:raw", seen={SCOPE: ["ccx-raw:gh-pr:main"]}): Allow(),
        Input(
            command="gh pr edit 123 --base dev  # ccx:raw",
            state=[
                ToolingRefusals(
                    refusals={"ccx-raw:gh-pr": Refusal(tool="ccx", action="gh\\ pr", evidence="x", lane=True)}
                )
            ],
        ): Allow(),
        Input(command="ccx vcs status", output="dev · clean"): Allow(),
        Input(
            command="cc-slack reply --url C1/p12 --text hi",
            output="posting\ncc-slack: no cc-slack session for this Claude window\n",
        ): Warn(pattern=r"^cc-slack refused: cc-slack: no cc-slack session for this Claude window\n"),
        Input(
            command="git grep -n 'no cc-slack session' go/cc-slack",
            output="go/cc-slack/ops.go:651: no cc-slack session for this Claude window",
        ): Allow(),
        Input(
            tool="Read",
            tool_input={"file_path": "/repo/ops.go"},
            output="no cc-slack session for this Claude window",
        ): Allow(),
    },
)
def record_refusal(evt: BaseHookEvent) -> HookResult | None:
    if (found := refusal(evt)) is None:
        return None
    key, record = found
    with ToolingRefusals.mutate(evt) as state:
        known = state.refusals.setdefault(key, record)
    if known.lane or not evt.ctx.s.once(f"{key}:{evt.agent_id or 'main'}", scope=SCOPE):
        return None
    return evt.warn(REFUSAL_MESSAGE.format(tool=known.tool, evidence=record.evidence, key=key))


RAW_REFUSED = ToolingRefusals(
    refusals={"ccx-raw:gh-pr": Refusal(tool="ccx", action=re.escape("gh pr"), evidence="gh pr edit 9  # ccx:raw")}
)
SLACK_REFUSED = ToolingRefusals(
    refusals={
        "cc-slack-session": Refusal(
            tool="cc-slack", action=CC_SLACK_ACTION.pattern, evidence="no cc-slack session for this Claude window"
        )
    }
)


@on(
    Event.PreToolUse,
    only_if=[Tool("Agent", "Task", "Skill")],
    skip_if=[FromSubagent()],
    tests={
        Input(
            tool="Agent",
            tool_input={"prompt": "Post the reply with cc-slack reply in C1/1.2", "subagent_type": "lane"},
            state=[SLACK_REFUSED],
        ): Block(pattern=r"`tooling-lane: cc-slack-session`"),
        Input(
            tool="Skill",
            tool_input={"skill": "cc-slack:slack", "args": "reply in the incident thread"},
            state=[SLACK_REFUSED],
        ): Block(pattern="already refused"),
        Input(
            tool="Agent",
            tool_input={
                "prompt": "Fix the cc-slack session lookup and release it.\ntooling-lane: cc-slack-session",
                "subagent_type": "lane",
            },
            state=[SLACK_REFUSED],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Post the reply with cc-slack reply in C1/1.2", "subagent_type": "lane"},
            state=[
                ToolingRefusals(
                    refusals={
                        "cc-slack-session": SLACK_REFUSED.refusals["cc-slack-session"].model_copy(update={"lane": True})
                    }
                )
            ],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Land the ledger stack", "subagent_type": "lane"},
            state=[SLACK_REFUSED],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Post the reply with cc-slack reply in C1/1.2", "subagent_type": "lane"},
            agent_id="a1b2c3",
            state=[SLACK_REFUSED],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Post the reply with cc-slack reply in C1/1.2", "subagent_type": "lane"},
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Retarget with gh pr edit 9 --base dev", "subagent_type": "lane"},
            state=[RAW_REFUSED],
        ): Block(pattern=r"`tooling-lane: ccx-raw:gh-pr`"),
        Input(
            tool="Agent",
            tool_input={"prompt": "Teach ccx to retarget.\ntooling-lane: ccx-raw:gh-pr", "subagent_type": "lane"},
            state=[RAW_REFUSED],
        ): Allow(),
    },
)
def block_repeated_dispatch(evt: BaseHookEvent) -> HookResult | None:
    text = dispatch_text(evt)
    if not (refusals := ToolingRefusals.load(evt).refusals):
        return None
    if marked := {found["key"] for found in LANE_MARKER.finditer(text)} & refusals.keys():
        with ToolingRefusals.mutate(evt) as state:
            for key in marked:
                state.refusals[key].lane = True
    for key, record in refusals.items():
        if key not in marked and not record.lane and re.search(record.action, text):
            return evt.block(REPEAT_MESSAGE.format(tool=record.tool, evidence=record.evidence, key=key))
    return None
