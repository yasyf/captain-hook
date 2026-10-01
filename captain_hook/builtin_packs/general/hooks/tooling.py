from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    Event,
    InlineTests,
    Input,
    OtherCall,
    PostToolUseFailureEvent,
    T,
    TaskCall,
    Tool,
    Warn,
    llm_nudge,
)

if TYPE_CHECKING:
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
    return evt.ctx.t.tool_calls.named("Bash").where_input(command=exact).count() >= REPEATS


def detect_bash(evt: BaseHookEvent) -> Detection | None:
    raw = evt.command.raw
    calls = evt.command.calls()
    head = calls[0].name if calls else ""
    if RAW_FALLBACK.search(raw):
        verb = " ".join([head, *calls[0].args[:1]]) if calls else "?"
        return Detection("`# ccx:raw` fallback", verb, raw)
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
    return (found := detect(evt)) is not None and evt.ctx.s.once(found.key, scope=SCOPE)


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
        contexts=[ToolingSignal()],
        agent=False,
        transcript=True,
        tests=tests,
    )


def bash_calls(command: str, n: int) -> list[dict[str, object]]:
    return [T.assistant(T.tool("Bash", command=command)) for _ in range(n)]


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
        ): Warn(pattern="tooling lane"),
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
        Input(command="gh pr edit 123 --base dev  # ccx:raw"): Warn(pattern="tooling lane"),
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
            command="orca terminal send w1 'status?'",
            transcript=bash_calls("orca terminal send w1 'status?'", 2),
        ): Allow(),
        Input(command="git status"): Allow(),
        Input(command="ccx vcs status"): Allow(),
        Input(tool="SendMessage", tool_input={"to": "team-lead", "message": "PR #12 merged."}): Allow(),
        Input(command="gh pr edit 123 --base dev  # ccx:raw", llm={"fire": False}): Allow(),
        Input(
            command="gh pr edit 123 --base dev  # ccx:raw",
            seen={SCOPE: ["`# ccx:raw` fallback:gh pr"]},
        ): Allow(),
        Input(
            command="gh pr edit 77 --base dev  # ccx:raw",
            seen={SCOPE: ["`# ccx:raw` fallback:gt track"]},
        ): Warn(pattern="tooling lane"),
    },
)
