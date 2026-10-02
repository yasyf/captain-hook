from __future__ import annotations

import os
from itertools import takewhile
from pathlib import PurePath
from signal import SIGKILL, SIGTERM, Signals
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    Event,
    HookResult,
    Input,
    LambdaCondition,
    ResourcePressureEvent,
    Warn,
    on,
)
from captain_hook.procwatch import judge, screen
from captain_hook.procwatch.identity import ProcessIdentity
from captain_hook.procwatch.ownership import Owned, prove
from captain_hook.procwatch.settings import PerformanceSettings
from captain_hook.procwatch.signal import Outcome, terminate
from captain_hook.procwatch.state import (
    Notice,
    ProcwatchState,
    StateUnavailable,
    announce,
    pending_notice,
    record_notice,
    record_refusal,
    record_signal,
    signaled,
    take_pending,
)
from captain_hook.util import proc
from captain_hook.util.proc import Unreadable

if TYPE_CHECKING:
    from collections.abc import Callable

UID = os.getuid()
CLAUDE_START = 1790845800
CHILD_START = 1790846401
TABLE = (
    "    1     0     1     0 Thu Oct  1 09:00:00 2026 /sbin/launchd\n"
    f"  500     1   500 {UID:>5} Thu Oct  1 09:10:00 2026 claude --effort xhigh\n"
    f"  600   500   600 {UID:>5} Thu Oct  1 09:20:00 2026 /bin/zsh -c rg -n TODO .\n"
    f"  601   600   600 {UID:>5} Thu Oct  1 09:20:01 2026 rg -n TODO .\n"
    f"  700   500   700 {UID:>5} Thu Oct  1 09:20:00 2026 /bin/zsh -c ./deploy.sh production\n"
    f"  701   700   700 {UID:>5} Thu Oct  1 09:20:01 2026 node build.js\n"
    f"  800   500   800 {UID:>5} Thu Oct  1 09:20:00 2026 claude -p review\n"
    f"  801   800   800 {UID:>5} Thu Oct  1 09:20:01 2026 rg -n TODO .\n"
    f"  900   500   900 {UID:>5} Thu Oct  1 09:20:01 2026 /bin/zsh -c tmux new -d\n"
    f"  901   900   901 {UID:>5} Thu Oct  1 09:20:01 2026 tmux new -d\n"
)
LIVE = {"ps -A -ww -o": TABLE}
FRESH = [ProcwatchState()]
NO_LIVE = {"CAPT_HOOK_TEST_NO_LIVE": "1"}
CHILDREN = {
    601: (600, 600, ["rg", "-n", "TODO", "."]),
    701: (700, 700, ["node", "build.js"]),
    801: (800, 800, ["rg", "-n", "TODO", "."]),
    900: (500, 900, ["/bin/zsh", "-c", "tmux", "new", "-d"]),
}
PENDING = Notice(id="n1", text="`rg` (pid 601) has run 3 min at 95% CPU.")


def pressure_payload(stage: str, pid: int, *, start_unix: int = CHILD_START) -> dict[str, Any]:
    ppid, pgid, argv = CHILDREN[pid]
    return {
        "hook_event_name": "ResourcePressure",
        "session_id": "fixture",
        "stage": stage,
        "claude_pid": 500,
        "claude_start_unix": CLAUDE_START,
        "process": {
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            "start_unix": start_unix,
            "start_usec": 0,
            "comm": argv[0],
            "argv": argv,
            "cwd": "/w",
            "runtime_s": 180.0,
            "cpu_fraction": 0.95,
            "disk_bps": 0.0,
            "ancestry": [],
        },
        "metrics": {"cpu": True, "disk": False},
    }


def program(evt: ResourcePressureEvent, identity: ProcessIdentity) -> str:
    return (PurePath(identity.argv[0]).name if identity.argv else evt.comm)[:40]


def refuse(evt: ResourcePressureEvent, identity: ProcessIdentity, reason: str) -> HookResult:
    record_refusal(evt, identity.key, text := screen.redact(f"Resource monitor left pid {evt.pid} running: {reason}"))
    return evt.block(text)


def vet(evt: ResourcePressureEvent, identity: ProcessIdentity) -> Owned | Unreadable:
    if (table := proc.process_table()) is None:
        return Unreadable("the process table is unreadable.")
    match prove(table, identity, claude_pid=evt.claude_pid, claude_start_unix=evt.claude_start_unix):
        case Owned(ancestry=ancestry) as owned:
            parents = takewhile(lambda entry: entry.pid != evt.claude_pid, ancestry)
            if (why := screen.excluded([identity.command, *(entry.command for entry in parents)])) is not None:
                return Unreadable(f"its command line {why}.")
            return owned
        case refused:
            return refused


def recheck(evt: ResourcePressureEvent, identity: ProcessIdentity) -> Callable[[], Unreadable | None]:
    return lambda: verdict if isinstance(verdict := vet(evt, identity), Unreadable) else None


def warning(evt: ResourcePressureEvent, identity: ProcessIdentity, settings: PerformanceSettings) -> str:
    usage = " and ".join(
        [
            *([f"{evt.cpu_fraction:.0%} CPU"] if evt.cpu_fraction is not None else []),
            *([f"{evt.disk_bps / 1_048_576:.0f} MB/s disk"] if evt.disk_bps is not None else []),
        ]
    )
    minutes = f"{evt.runtime_s / 60:.0f} min"
    head = f"`{program(evt, identity)}` (pid {evt.pid}) has run {minutes} at {usage or 'heavy load'}."
    if settings.terminate:
        return (
            f"{head} It is stopped in {settings.grace_seconds} s if a small model rates it disposable; "
            "set `HOOKS_PERFORMANCE_TERMINATE=false` to only warn."
        )
    return f"{head} Termination is off, so it runs until it finishes or you stop it."


def report(evt: ResourcePressureEvent, identity: ProcessIdentity, sig: Signals, outcome: Outcome) -> HookResult:
    if not outcome.signaled:
        return refuse(evt, identity, f"{outcome.reason}.")
    text = f"Resource monitor sent {sig.name} to `{program(evt, identity)}` (pid {evt.pid}) after its grace period."
    record_signal(evt, identity.key, sig, text)
    announce("Resource monitor stopped a process", text)
    return evt.warn(screen.redact(text))


def send(evt: ResourcePressureEvent, identity: ProcessIdentity, sig: Signals) -> HookResult:
    return report(evt, identity, sig, terminate(identity, sig, recheck=recheck(evt, identity)))


def warn_stage(evt: ResourcePressureEvent, identity: ProcessIdentity, settings: PerformanceSettings) -> HookResult:
    if isinstance(verdict := vet(evt, identity), Unreadable):
        return refuse(evt, identity, verdict.reason)
    record_notice(evt, text := screen.redact(warning(evt, identity, settings)))
    announce("Resource-heavy process", text)
    return evt.warn(text)


def judge_stage(evt: ResourcePressureEvent, identity: ProcessIdentity, settings: PerformanceSettings) -> HookResult:
    if isinstance(verdict := vet(evt, identity), Unreadable):
        return refuse(evt, identity, verdict.reason)
    if not settings.terminate:
        return refuse(evt, identity, "termination is off.")
    if signaled(evt, identity.key, SIGTERM):
        return evt.block(f"Resource monitor already sent SIGTERM to pid {evt.pid}, so it does not judge it again.")
    match judge.disposable(evt, settings, identity, judge.JudgeFacts.of(evt, identity, verdict)):
        case Unreadable(reason=reason):
            return refuse(evt, identity, reason)
        case False:
            return refuse(evt, identity, "a small model did not rate it disposable.")
        case True:
            return send(evt, identity, SIGTERM)


def escalate_stage(evt: ResourcePressureEvent, identity: ProcessIdentity, settings: PerformanceSettings) -> HookResult:
    if not signaled(evt, identity.key, SIGTERM):
        return evt.block(f"Resource monitor never sent SIGTERM to pid {evt.pid}, so it does not escalate.")
    if settings.escalate_after_seconds <= 0:
        return refuse(evt, identity, "escalation is off.")
    if isinstance(verdict := vet(evt, identity), Unreadable):
        return refuse(evt, identity, verdict.reason)
    return send(evt, identity, SIGKILL)


STAGES = {"warn": warn_stage, "judge": judge_stage, "escalate": escalate_stage}


@on(
    Event.ResourcePressure,
    skip_planning_agents=False,
    tests={
        Input(tool_input=pressure_payload("warn", 601), commands=LIVE, env=NO_LIVE, state=FRESH): Warn(
            pattern=r"pid 601\) has run 3 min at 95% CPU"
        ),
        Input(tool_input=pressure_payload("warn", 701), commands=LIVE, env=NO_LIVE, state=FRESH): Block(
            pattern="deploy, release, or publish"
        ),
        Input(tool_input=pressure_payload("warn", 900), commands=LIVE, env=NO_LIVE, state=FRESH): Block(
            pattern="terminal multiplexer"
        ),
        Input(tool_input=pressure_payload("warn", 801), commands=LIVE, env=NO_LIVE, state=FRESH): Block(
            pattern="nested agent"
        ),
        Input(
            tool_input=pressure_payload("warn", 601, start_unix=CHILD_START - 1),
            commands=LIVE,
            env=NO_LIVE,
            state=FRESH,
        ): Block(pattern="reused"),
        Input(tool_input=pressure_payload("warn", 601), commands=LIVE, env=NO_LIVE): Block(
            pattern="no state directory"
        ),
        Input(
            tool_input=pressure_payload("judge", 601),
            commands=LIVE,
            env=NO_LIVE,
            state=FRESH,
            llm={"disposable": False},
        ): Block(pattern="did not rate it disposable"),
        Input(
            tool_input=pressure_payload("judge", 601),
            commands=LIVE,
            env=NO_LIVE,
            state=[ProcwatchState(judging=[f"601:{CHILD_START}"], judge_calls=1)],
            llm={"disposable": True},
        ): Block(pattern="already in flight"),
        Input(
            tool_input=pressure_payload("judge", 601),
            commands=LIVE,
            env=NO_LIVE,
            state=FRESH,
            llm={"disposable": True},
        ): Block(pattern="disabled under test"),
        Input(tool_input=pressure_payload("escalate", 601), commands=LIVE, env=NO_LIVE, state=FRESH): Block(
            pattern="never sent SIGTERM"
        ),
    },
)
def pressure(evt: ResourcePressureEvent) -> HookResult:
    try:
        settings = PerformanceSettings()
    except ValidationError:
        return evt.block("Resource monitor settings are invalid; fix the `HOOKS_PERFORMANCE_*` values to re-enable it.")
    if not settings.enabled:
        return evt.block("Resource monitor is off; set `HOOKS_PERFORMANCE_ENABLED=true` to turn it on.")
    identity = ProcessIdentity.from_payload(evt.process)
    try:
        return STAGES[evt.stage](evt, identity, settings)
    except StateUnavailable as exc:
        return evt.block(f"Resource monitor left pid {evt.pid} running: {exc.reason}")


@on(
    Event.PreToolUse | Event.PostToolUse | Event.UserPromptSubmit,
    only_if=[LambdaCondition(pending_notice)],
    tests={
        Input(command="ls", state=[ProcwatchState(pending=[PENDING])]): Warn(system_message="pid 601"),
        Input(command="ls", state=[ProcwatchState(pending=[PENDING], delivered=["n1"])]): Allow(),
        Input(command="ls"): Allow(),
    },
)
def deliver(evt: BaseHookEvent) -> HookResult | None:
    if not (notices := take_pending(evt)):
        return None
    text = "\n\n".join(notice.text for notice in notices)
    return evt.context(text, system_message=text)
