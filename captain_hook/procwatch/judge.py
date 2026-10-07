from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import TYPE_CHECKING

from spawnllm import Binary, BinaryAnswer

from captain_hook.procwatch import screen
from captain_hook.procwatch.state import claim_judgement, release_judgement, settle_judgement
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from captain_hook.events import ResourcePressureEvent
    from captain_hook.procwatch.identity import ProcessIdentity
    from captain_hook.procwatch.ownership import Owned
    from captain_hook.procwatch.settings import PerformanceSettings
    from captain_hook.util.proc import Unreadable

MAX_CONTEXT_CHARS = 600
DISPOSABLE = 0.9
QUESTIONS = {
    "disposable": Binary(
        "Is this process only a search, test run, lint, type check, build, or analysis, so that stopping it loses no "
        "durable work and the agent can simply rerun it?",
        yes="Stopping it loses nothing; the agent reruns it.",
        no="It deploys, releases, publishes, pushes, writes shared state, installs, serves, watches, edits files, or "
        "its purpose is unclear.",
    ),
}


@dataclass(frozen=True, slots=True)
class JudgeFacts:
    command_redacted: str
    argv0: str
    cwd_redacted: str | None
    runtime_s: float
    cpu_fraction: float | None
    disk_bps: float | None
    ancestry_argv0s: tuple[str, ...]

    @classmethod
    def of(cls, evt: ResourcePressureEvent, identity: ProcessIdentity, owned: Owned) -> JudgeFacts:
        return cls(
            command_redacted=screen.redact_argv(identity.argv),
            argv0=screen.redact(PurePath(identity.argv[0]).name if identity.argv else evt.comm),
            cwd_redacted=screen.redact(identity.cwd) if identity.cwd else None,
            runtime_s=evt.runtime_s,
            cpu_fraction=evt.cpu_fraction,
            disk_bps=evt.disk_bps,
            ancestry_argv0s=tuple(screen.redact(entry.argv0) for entry in owned.ancestry),
        )

    def render(self) -> str:
        lines = [
            f"program: {self.argv0}",
            f"runtime: {self.runtime_s / 60:.1f} min",
            *([f"cpu: {self.cpu_fraction:.0%} of one core"] if self.cpu_fraction is not None else []),
            *([f"disk: {self.disk_bps / 1_048_576:.1f} MB/s"] if self.disk_bps is not None else []),
            *([f"cwd: {self.cwd_redacted}"] if self.cwd_redacted else []),
            f"parents: {' <- '.join(self.ancestry_argv0s)}",
            f"command: {self.command_redacted}",
        ]
        return "\n".join(lines)[:MAX_CONTEXT_CHARS]


def ask(evt: ResourcePressureEvent, settings: PerformanceSettings, facts: JudgeFacts) -> bool:
    decision = evt.decide(facts.render(), QUESTIONS, timeout=settings.judge_timeout_seconds)
    match decision and decision.answers["disposable"]:
        case BinaryAnswer(p_yes=p_yes):
            return p_yes >= DISPOSABLE
        case _:
            return False


def disposable(
    evt: ResourcePressureEvent, settings: PerformanceSettings, identity: ProcessIdentity, facts: JudgeFacts
) -> bool | Unreadable:
    if (claimed := claim_judgement(evt, identity.key, cap=settings.max_judge_calls_per_session)) is not None:
        return claimed
    try:
        verdict = ask(evt, settings, facts)
    except reqenv.Abandoned:
        release_judgement(evt, identity.key)
        raise
    settle_judgement(evt, identity.key, verdict)
    reqenv.checkpoint()
    return verdict
