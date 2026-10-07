from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from captain_hook import (
    Allow,
    Block,
    Confirm,
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
    from captain_hook.cmd import Call

TOOLING = frozenset({"ccx", "gt", "orca", "ccn", "cc-notes", "cc-slack", "codex-ask", "capt-hook"})
TOOL_WORD = re.compile(rf"(?<![\w-])({'|'.join(sorted(map(re.escape, TOOLING)))})(?![\w-])")
MANUAL = re.compile(r"(?i)\b(?:work[-\s]?around|by hand|manually)\b")
REFUSAL_LINE = re.compile(r"(?i)\b(?:usage|refus\w*|unknown (?:command|flag|option)|unrecognized)\b")
LANE_REPORT = re.compile(r"(?i)\bccx refusal:|\btooling defect\b")
EXIT_HEADER = re.compile(r"\A(?:Error: )?Exit code \d+\n")
REPEATS = 3
SCOPE = "tooling_nudge"
EVIDENCE_CHARS = 240
RESET = re.compile(r"rate-limited until (?P<at>\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d))")

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
    "A first-party tool fell short and the agent worked around it. "
    "Spawn a tooling lane that fixes and releases the tool, or run `ccn papercut` if you cannot spawn agents."
)


@dataclass(frozen=True, slots=True)
class Verb:
    words: tuple[str, ...]

    def __init__(self, *words: str) -> None:
        object.__setattr__(self, "words", words)

    @property
    def text(self) -> str:
        return " ".join(self.words)

    def ran(self, evt: BaseHookEvent) -> bool:
        match evt.input:
            case OtherCall(name=name) if name.startswith("mcp__"):
                return name.rpartition("__")[2] == self.text
        return bool(evt.command) and any(argv(call)[: len(self.words)] == self.words for call in evt.command.calls())

    def named_in(self, text: str) -> bool:
        return re.search(rf"(?<![\w-]){r'\s+'.join(map(re.escape, self.words))}(?![\w-])", text) is not None


@dataclass(frozen=True, slots=True)
class Signature:
    key: str
    text: re.Pattern[str]
    verbs: tuple[Verb, ...]
    lane_name: re.Pattern[str]
    window: timedelta | None = None
    failures_only: bool = False

    def source(self, evt: BaseHookEvent) -> Verb | None:
        return next((verb for verb in self.verbs if verb.ran(evt)), None)

    def refused_text(self, evt: BaseHookEvent) -> str:
        if not self.failures_only:
            return result_text(evt)
        if isinstance(evt, PostToolUseFailureEvent):
            return evt.error
        match getattr(evt, "tool_response", None):
            case {"stderr": str() as stderr}:
                return stderr
        return ""

    def expires(self, text: str) -> datetime | None:
        if found := RESET.search(text):
            return datetime.fromisoformat(found["at"])
        return datetime.now(UTC) + self.window if self.window else None


CC_SLACK_VERBS = (
    *(Verb("cc-slack", verb) for verb in ("send", "reply", "edit", "react", "watch")),
    *(Verb(f"slack_{verb}") for verb in ("send", "reply", "edit", "react", "watch", "dm_status")),
    Verb("cc-slack:slack"),
)
GITHUB_VERBS = (
    Verb("ccx", "vcs", "pr"),
    Verb("ccx", "vcs", "status"),
    Verb("ccx", "vcs", "reviews"),
    Verb("gh", "pr"),
    Verb("gh", "api"),
    Verb("gh", "run"),
    Verb("stack-enqueue"),
)

SIGNATURES = (
    Signature(
        "cc-slack-session",
        re.compile(r"no cc-slack session for this Claude window"),
        CC_SLACK_VERBS,
        re.compile(r"cc-slack-session"),
    ),
    Signature(
        "cc-slack-no-watch",
        re.compile(r"\bpass `?no_watch\b"),
        CC_SLACK_VERBS,
        re.compile(r"cc-slack-no-watch"),
    ),
    Signature(
        "github-quota",
        re.compile(
            r"(?i)rate-limited until|api rate limit exceeded|secondary rate limit"
            r"|graphql\b[^\n]{0,60}\b(?:quota|rate limit)"
        ),
        GITHUB_VERBS,
        re.compile(r"(?<![\w])(?:gh|github|graphql)-(?:quota|rate-limit)"),
        timedelta(hours=1),
        failures_only=True,
    ),
)


class Refusal(BaseModel):
    tool: str
    verbs: tuple[str, ...]
    evidence: str
    expires: datetime | None

    def live(self, now: datetime) -> bool:
        return self.expires is None or self.expires > now

    def repeated_in(self, text: str) -> bool:
        return any(Verb(*verb.split()).named_in(text) for verb in self.verbs)


@workflow_state("tooling_refusals")
class ToolingRefusals(WorkflowState):
    refusals: dict[str, Refusal] = {}
    lanes: set[str] = set()


REFUSAL_MESSAGE = (
    "`{tool}` refused this action: spawn a tooling lane that fixes it with the line `ccx: tooling-lane={key}` "
    "in its prompt, "
    "or report the refusal to your orchestrator if you cannot spawn agents. "
    "Repeats of the dispatch stay blocked until the lane exists."
)
REPEAT_MESSAGE = (
    "This dispatch repeats an action `{tool}` already refused, and no tooling lane exists for it. "
    "Spawn the tooling lane with the line `ccx: tooling-lane={key}` in its prompt, then retry."
)
REPEAT_RULE = (
    "This dispatch asks for the same action the first-party tool refused (`{evidence}`), "
    "rather than an unrelated task that mentions similar words."
)


def argv(call: Call) -> tuple[str, ...]:
    return (Path(call.name).name, *call.verb_argv[1:])


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
    for signature in SIGNATURES:
        if (ran := signature.source(evt)) and (found := signature.text.search(text := signature.refused_text(evt))):
            return signature.key, Refusal(
                tool=ran.text,
                verbs=tuple(verb.text for verb in signature.verbs),
                evidence=excerpt(text, found),
                expires=signature.expires(text),
            )
    return None


def dispatch_text(evt: BaseHookEvent) -> str:
    match evt.input:
        case TaskCall(prompt=prompt):
            return prompt
        case SkillCall(skill=skill, args=args):
            return f"{skill} {args or ''}"
    return ""


def lane_keys(evt: BaseHookEvent) -> set[str]:
    keys = {key} if (key := evt.annotations.get("tooling-lane")) else set()
    match evt.input:
        case TaskCall(agent_name=name) if name:
            keys |= {signature.key for signature in SIGNATURES if signature.lane_name.search(name)}
    return keys


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


LIVE = datetime(2099, 1, 1, tzinfo=UTC)
RESET_PASSED = datetime(2026, 1, 1, tzinfo=UTC)
QUOTA = Refusal(
    tool="ccx vcs pr",
    verbs=tuple(verb.text for verb in GITHUB_VERBS),
    evidence="ccx: GitHub GraphQL quota exhausted",
    expires=LIVE,
)
QUOTA_REFUSED = ToolingRefusals(refusals={"github-quota": QUOTA})
SLACK_REFUSED = ToolingRefusals(
    refusals={
        "cc-slack-session": Refusal(
            tool="cc-slack",
            verbs=tuple(verb.text for verb in CC_SLACK_VERBS),
            evidence="no cc-slack session for this Claude window",
            expires=None,
        )
    }
)
PR_236_BODY = (
    "Context: The deterministic tooling-refusal path added in #234 blocked an unrelated cc-slack CLI\n"
    "rebuild dispatch because its prompt included `ccx vcs worktree add`. The quota record came from\n"
    '`ccx code read` printing an inline test containing "GitHub GraphQL quota exhausted"; a documentation\n'
    "excerpt about `rate-limited until` produced another false record and blocked a PR watcher.\n"
)
CC_SLACK_CLI_SYNC = (
    "You are cc-slack-cli-sync. Defect: every cc-slack CLI call from lanes now fails: the daemon runs a newer "
    "release than this CLI. Use a fresh worktree `ccx vcs worktree add cc-slack-cli-sync` if you need to build, "
    "install the CLI as the new symlink target, and verify `cc-slack --version` matches the daemon and "
    "`cc-slack send --help` lists `--grant`."
)


@on(
    Event.PostToolUse | Event.PostToolUseFailure,
    tests={
        Input(
            tool="mcp__plugin_cc-slack_cc-slack__slack_reply",
            tool_input={"channel_id": "C1", "thread_ts": "1.2", "text": "hi"},
            output="no cc-slack session for this Claude window; run cc-slack login",
        ): Warn(pattern="^`slack_reply` refused.*`ccx: tooling-lane=cc-slack-session`"),
        Input(
            tool="Agent",
            tool_input={"prompt": "reply in the thread", "subagent_type": "lane"},
            output="Could not post: cc-slack says to pass no_watch when the thread is already watched.",
        ): Allow(),
        Input(
            tool="mcp__plugin_cc-slack_cc-slack__slack_thread",
            tool_input={"channel_id": "C1", "thread_ts": "1.2"},
            output="U1: the bot says no cc-slack session for this Claude window",
        ): Allow(),
        Input(
            command="ccx vcs pr status 12",
            output="ccx: GitHub GraphQL quota exhausted; rate-limited until 2026-10-01T22:05:00Z",
        ): Allow(),
        Input(
            command="gh pr view 236 --repo yasyf/captain-hook --json body -q .body",
            output=PR_236_BODY,
        ): Allow(),
        Input(
            command="ccx code read captain_hook/builtin_packs/general/hooks/tooling.py --section 400-415",
            output='→ [408#bbgx]         ): Warn(pattern=r"^ccx refused: ccx: GitHub GraphQL quota exhausted"),',
        ): Allow(),
        Input(
            command="ccx code read docs/pr-status.md",
            output="`pr watch` prints `rate-limited until <next probe>` and sleeps until the GraphQL quota resets.",
        ): Allow(),
        Input(command="gh pr edit 123 --base dev  # ccx:raw"): Allow(),
        Input(command="cd wt && gt submit --no-interactive  # ccx:raw"): Allow(),
        Input(
            command="cd /wt && git rebase origin/dev 2>&1 | tail -5; git status -s | head # ccx:raw",
            output="Could not apply 25737e453d... cc-slack: hand a refused op to the newest local build",
        ): Allow(),
        Input(command="gh pr edit 123 --base dev  # ccx:raw", env={"CAPT_HOOK_CCX_RAW": "1"}): Allow(),
        Input(command="ccx vcs status", output="dev · clean"): Allow(),
        Input(
            command="cc-slack reply --url C1/p12 --text hi",
            output="posting\ncc-slack: no cc-slack session for this Claude window\n",
        ): Warn(pattern="^`cc-slack reply` refused this action"),
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
        state.refusals[key] = record
        covered = key in state.lanes
    if covered or not evt.ctx.s.once(f"{key}:{evt.agent_id or 'main'}", scope=SCOPE):
        return None
    return evt.warn(REFUSAL_MESSAGE.format(tool=record.tool, key=key))


@on(
    Event.PreToolUse,
    only_if=[Tool("Agent", "Task", "Skill")],
    tests={
        Input(
            tool="Agent",
            tool_input={"prompt": "Fix the GraphQL fallback.", "name": "gh-quota-once-and-for-all"},
        ): Allow(),
        Input(tool="Agent", tool_input={"prompt": "Fix cc-slack.\nccx: tooling-lane=cc-slack-session"}): Allow(),
    },
)
def record_lane(evt: BaseHookEvent) -> HookResult | None:
    if keys := lane_keys(evt) - ToolingRefusals.load(evt).lanes:
        with ToolingRefusals.mutate(evt) as state:
            state.lanes |= keys
    return None


@on(
    Event.PreToolUse,
    only_if=[Tool("Agent", "Task", "Skill")],
    skip_if=[FromSubagent()],
    tests={
        Input(
            tool="Agent",
            tool_input={"prompt": "Post the reply with cc-slack reply in C1/1.2", "subagent_type": "lane"},
            state=[SLACK_REFUSED],
        ): Block(pattern=r"`ccx: tooling-lane=cc-slack-session`"),
        Input(
            tool="Skill",
            tool_input={"skill": "cc-slack:slack", "args": "reply in the incident thread"},
            state=[SLACK_REFUSED],
        ): Block(pattern="already refused"),
        Input(
            tool="Agent",
            tool_input={
                "prompt": "Fix the cc-slack session lookup and release it.\nccx: tooling-lane=cc-slack-session",
                "subagent_type": "lane",
            },
            state=[SLACK_REFUSED],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Post the reply with cc-slack reply in C1/1.2", "subagent_type": "lane"},
            state=[SLACK_REFUSED.model_copy(update={"lanes": {"cc-slack-session"}})],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Land the ledger stack", "subagent_type": "lane"},
            state=[SLACK_REFUSED],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": CC_SLACK_CLI_SYNC, "name": "cc-slack-cli-sync"},
            state=[SLACK_REFUSED],
            llm={"block": False, "confident": True},
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": CC_SLACK_CLI_SYNC, "name": "cc-slack-cli-sync"},
            state=[SLACK_REFUSED],
            llm={"block": True, "confident": False},
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
            tool_input={"prompt": "Post with cc-slack reply.\nccx: tooling-lane=cc-slack-session", "name": "fix"},
            state=[SLACK_REFUSED],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Post with cc-slack reply.\ntooling-lane: cc-slack-session", "name": "fix"},
            state=[SLACK_REFUSED],
        ): Block(pattern="already refused"),
        Input(
            tool="Agent",
            tool_input={"prompt": "Post with cc-slack reply.\nccx: tooling-lane=github-quota", "name": "fix"},
            state=[SLACK_REFUSED],
        ): Block(pattern=r"`ccx: tooling-lane=cc-slack-session`"),
        Input(
            tool="Agent",
            tool_input={"prompt": "Poll `ccx vcs pr status 28999` until it lands.", "name": "pr-28999-watch"},
            state=[QUOTA_REFUSED],
        ): Block(pattern=r"`ccx: tooling-lane=github-quota`"),
        Input(
            tool="Agent",
            tool_input={"prompt": CC_SLACK_CLI_SYNC, "name": "cc-slack-cli-sync"},
            state=[QUOTA_REFUSED],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Poll `ccx vcs pr status 28999` until it lands.", "name": "pr-28999-watch"},
            state=[ToolingRefusals(refusals={"github-quota": QUOTA.model_copy(update={"expires": RESET_PASSED})})],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Poll `ccx vcs pr status 28999` until it lands.", "name": "pr-28999-watch"},
            state=[QUOTA_REFUSED.model_copy(update={"lanes": {"github-quota"}})],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Move ccx off GraphQL; verify with gh api rate_limit.", "name": "gh-quota-fix"},
            state=[QUOTA_REFUSED],
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "ccx: tooling-lane=github-quota\nVerify with `gh api rate_limit`.", "name": "quota"},
            state=[QUOTA_REFUSED],
        ): Allow(),
    },
)
def block_repeated_dispatch(evt: BaseHookEvent) -> HookResult | None:
    state = ToolingRefusals.load(evt)
    covered = state.lanes | lane_keys(evt)
    text = dispatch_text(evt)
    now = datetime.now(UTC)
    for key, record in state.refusals.items():
        if key not in covered and record.live(now) and record.repeated_in(text):
            return evt.block(
                REPEAT_MESSAGE.format(tool=record.tool, key=key),
                confirm=Confirm(rule=REPEAT_RULE.format(evidence=record.evidence)),
            )
    return None
