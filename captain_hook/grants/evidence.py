"""Where the owner's words live, and the bar for what counts as them.

Only the owner's own words are evidence: the root session's prompts (a lane's own prompts are its
brief and teammates' messages), messages the owner queued while the agent worked, and the owner's
AskUserQuestion answers. Teammate messages, tool output, notifications, channel lines, and inbox
lines are never collected. A cc-notes answer counts only if it was last written before the acting
session started, so an agent cannot record a ruling and spend it.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from cc_transcript.models import AttachmentEvent, QueuedCommand
from cc_transcript.tools import AskUserQuestionResult, parse_tool_result

from captain_hook.grants.records import Evidence, Proposal
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

OWNER_WINDOW = 60
RULINGS_TIMEOUT = 5
OPTION_NUMBER = re.compile(r"\s*(\d+)\b")


class EvidenceSource(Protocol):
    """Collects the owner's words relevant to *action*, each with a stable id the judge can cite."""

    def collect(self, evt: BaseHookEvent, action: Proposal) -> list[Evidence]: ...


def tree_of(evt: BaseHookEvent) -> str:
    """The root session id of *evt*'s session tree: the spawning session for a lane, else its own."""
    return evt.ctx.root_path.stem if evt.ctx.root_path is not None else evt.session_id


@cache
def first_timestamp(path: Path) -> datetime:
    with path.open() as lines:
        for line in lines:
            if stamp := json.loads(line).get("timestamp"):
                return datetime.fromisoformat(stamp)
    raise ValueError(f"{path} records no timestamp")


def session_started(evt: BaseHookEvent) -> datetime:
    """When the acting session wrote its first transcript line: read from its file, or from an in-memory session."""
    if (path := evt.ctx.transcript_path) is not None:
        return first_timestamp(Path(path))
    return evt.ctx.transcript.events[0].meta.timestamp


def owner_sessions(evt: BaseHookEvent, needles: Sequence[str] = ()) -> list[tuple[Any, bool]]:
    """The sessions to read the owner's words from, each with whether its prompts are the owner's."""
    if (root := evt.ctx.root_transcript) is None:
        return [(evt.ctx.transcript, True)]
    excerpt = evt.ctx.root_excerpt(needles) if needles else None
    return [*([(excerpt, True)] if excerpt is not None else []), (root, True), (evt.ctx.transcript, False)]


def owner_uses(evt: BaseHookEvent, needles: Sequence[str] = (), window: int = OWNER_WINDOW) -> list[Any]:
    uses = {
        use.ref.tool_use_id or f"{use.ts}": use
        for session, _ in owner_sessions(evt, needles)
        for turn in session.recent_messages(window).turns
        for use in turn.tool_uses
    }
    return sorted(uses.values(), key=lambda use: use.result_ts or use.ts)


def parse_answer(payload: Any) -> AskUserQuestionResult | None:
    if not isinstance(payload, dict):
        return None
    answer = parse_tool_result("AskUserQuestion", payload, on_error="other")
    return answer if isinstance(answer, AskUserQuestionResult) else None


def answered(use: Any) -> tuple[dict[str, Any], AskUserQuestionResult] | None:
    """The question payload and the owner's answer, for a finished AskUserQuestion call."""
    if use.call.name != "AskUserQuestion" or use.result is None or use.result.is_error:
        return None
    payload = use.result.tool_use_result
    return (payload, answer) if (answer := parse_answer(payload)) is not None else None


def asked_options(payload: dict[str, Any], question: str) -> list[dict[str, Any]]:
    return next((q for q in payload.get("questions", []) if q.get("question") == question), {}).get("options", [])


def chosen_option(payload: dict[str, Any], question: str, label: str) -> dict[str, Any]:
    """The option *label* picks: by label, or by a leading option number in a typed answer."""
    options = asked_options(payload, question)
    if (named := next((o for o in options if o.get("label") == label), None)) is not None:
        return named
    if (number := OPTION_NUMBER.match(label)) is not None and 1 <= int(number[1]) <= len(options):
        return options[int(number[1]) - 1]
    return {}


def answer_detail(payload: dict[str, Any], question: str, label: str, answer: AskUserQuestionResult) -> str:
    picked = chosen_option(payload, question, label)
    annotation = answer.annotations.get(question)
    lines = [f"question: {question}"]
    for option in asked_options(payload, question):
        mark = "chosen" if option is picked else "shown"
        lines.append(f"option ({mark}) {option.get('label', '')}: {option.get('description', '')}")
        if isinstance(preview := option.get("preview"), str):
            lines.append(f"  preview: {preview}")
    if picked.get("label") != label:
        lines.append(f'typed: "{label}"')
    if annotation is not None and annotation.preview:
        lines.append(f"selected preview: {annotation.preview}")
    if annotation is not None and annotation.notes:
        lines.append(f"notes: {annotation.notes}")
    return "\n".join(lines)


def answer_words(answer: AskUserQuestionResult, question: str, label: str) -> str:
    annotation = answer.annotations.get(question)
    return "\n".join([label, *([annotation.notes] if annotation is not None and annotation.notes else [])])


def answer_evidence(ref: str, payload: dict[str, Any], answer: AskUserQuestionResult, at: datetime) -> list[Evidence]:
    """One evidence item per question the owner answered in the AskUserQuestion call *ref*."""
    return [
        Evidence(
            id=f"ask:{ref}#{index}",
            source="ask",
            quote=answer_words(answer, question, label),
            said_at=at,
            detail=answer_detail(payload, question, label, answer),
            key=f"ask:{ref}#{index}",
        )
        for index, (question, label) in enumerate(answer.answers.items())
    ]


def ask_evidence(use: Any) -> list[Evidence]:
    if (result := answered(use)) is None:
        return []
    return answer_evidence(use.ref.tool_use_id or f"{use.ts:%s}", *result, use.result_ts or use.ts)


def recorded_asks(evt: BaseHookEvent) -> list[Evidence]:
    from captain_hook.grants import store

    at = store.now()
    return [
        item
        for grant in store.grants("ask", tree_of(evt))
        if grant.revoked is None and (grant.expires is None or grant.expires > at)
        for item in grant.evidence
    ]


@dataclass(frozen=True, slots=True)
class Asked:
    """The owner's AskUserQuestion answers in the session tree, with every option and preview they saw."""

    needles: Callable[[BaseHookEvent], Sequence[str]] | None = None
    window: int = OWNER_WINDOW

    def collect(self, evt: BaseHookEvent, action: Proposal) -> list[Evidence]:
        needles = self.needles(evt) if self.needles else ()
        read = {item.id: item for use in owner_uses(evt, needles, self.window) for item in ask_evidence(use)}
        merged = {item.id: item for item in recorded_asks(evt)} | read
        return sorted(merged.values(), key=lambda item: item.said_at.timestamp() if item.said_at else 0.0)


def queued_words(turn: Any) -> list[tuple[str, Any]]:
    return [
        (event.detail.prompt or "", event)
        for event in turn.events
        if isinstance(event, AttachmentEvent)
        and isinstance(event.detail, QueuedCommand)
        and event.detail.origin == "human"
    ]


@dataclass(frozen=True, slots=True)
class OwnerWords:
    """What the owner typed, pasted, or queued in the session tree, minus machine-written turns.

    Attributes:
        machine: Markers of a prompt a tool wrote on the owner's behalf; such a prompt is never their words.
        needles: Maps the event to text whose root-session mentions are read however far back they sit.
        window: How many recent turns of each session to read.
    """

    machine: tuple[str, ...] = ()
    needles: Callable[[BaseHookEvent], Sequence[str]] | None = None
    window: int = OWNER_WINDOW

    def collect(self, evt: BaseHookEvent, action: Proposal) -> list[Evidence]:
        items: dict[str, Evidence] = {}
        needles = self.needles(evt) if self.needles else ()
        for session, prompts_are_owner in owner_sessions(evt, needles):
            for turn in session.recent_messages(self.window).turns:
                said = [(turn.prompt, turn.started_at)] if prompts_are_owner else []
                said += [(text, event.meta.timestamp) for text, event in queued_words(turn)]
                for text, at in said:
                    if text and not any(marker in text for marker in self.machine):
                        key = f"words:{sha256(text.encode()).hexdigest()[:12]}"
                        items[key] = Evidence(id=key, source="words", quote=text, said_at=at, key=key)
        return sorted(items.values(), key=lambda item: item.said_at.timestamp() if item.said_at else 0.0)


def squeezed(text: str) -> str:
    return " ".join(text.split())


def verbatim(quote: str, items: Sequence[Evidence]) -> Evidence | None:
    """The owner evidence that contains *quote* verbatim, whitespace folded, or ``None``."""
    folded = squeezed(quote)
    return next((item for item in items if folded and folded in squeezed(item.quote)), None) if folded else None


def ccn_answers(evt: BaseHookEvent, term: str) -> list[dict[str, Any]]:
    argv = ["ccn", "answer", "search", term, "--json", "--limit", "0", "-R", str(evt.cwd or reqenv.cwd())]
    done = subprocess.run(
        argv, capture_output=True, text=True, timeout=RULINGS_TIMEOUT, env=reqenv.env_map(), check=False
    )
    if done.returncode != 0 or not done.stdout.strip():
        return []
    return json.loads(done.stdout)


def ccn_answer(evt: BaseHookEvent, answer_id: str) -> dict[str, Any]:
    argv = ["ccn", "answer", "show", answer_id, "--json", "-R", str(evt.cwd or reqenv.cwd())]
    done = subprocess.run(
        argv, capture_output=True, text=True, timeout=RULINGS_TIMEOUT, env=reqenv.env_map(), check=True
    )
    return json.loads(done.stdout)


def written_at(answer: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(answer["updated_at"])


@dataclass(frozen=True, slots=True)
class Rulings:
    """The owner's durable rulings recorded as cc-notes answers that name the action's *search* term.

    Only answers last written before the acting session started count; each is pinned to that revision.
    """

    search: Callable[[Proposal], str]
    started: Callable[[BaseHookEvent], datetime] = field(default=session_started)

    def collect(self, evt: BaseHookEvent, action: Proposal) -> list[Evidence]:
        if not (term := self.search(action)):
            return []
        cutoff = self.started(evt)
        return [
            Evidence(
                id=f"ccn:{answer['id'][:7]}",
                source="ccn-answer",
                quote=answer["body"],
                said_at=written_at(answer),
                detail=f"ruling {answer['id'][:7]}: {answer['title']}",
                key=f"ccn:{answer['id']}@{written_at(answer).isoformat()}",
            )
            for answer in ccn_answers(evt, term)
            if term in answer.get("body", "") and written_at(answer) < cutoff
        ]
