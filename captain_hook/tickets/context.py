"""What a rule decides over: the action, and everything the session tree knows about the user's wishes.

Only the user's own words count as theirs: the root session's prompts (a lane's own prompts are its
brief and teammates' messages), messages the user queued while the agent worked, what the user typed
when rejecting a plan in the root session, their ``AskUserQuestion`` answers, and their picks on a
cc-present board. Teammate messages, tool output, notifications, channel lines, and inbox lines are
never the user's words. A cc-notes answer counts only if it was last written before the acting
session started, so an agent cannot record a ruling and act on it.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache, cached_property
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from cc_transcript.activity import ToolUse, Turn
from cc_transcript.filterspec import DENIAL_KIND_USER_REJECTED, USER_SAID_MARKER, USER_SAID_TRAILER
from cc_transcript.models import AssistantEvent, AttachmentEvent, QueuedCommand
from cc_transcript.tools import AskUserQuestionResult, parse_tool_result

from captain_hook.tickets.records import Answer, Evidence, Proposal
from captain_hook.transcripts import LazyTranscript
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from cc_transcript.query import Session

    from captain_hook.events import BaseHookEvent
    from captain_hook.snapshots.client import RemoteSession

WINDOW = 60
REPLIED_SHOWN = 400
RULINGS_TIMEOUT = 5
CCN_NOT_FOUND = 3
PRESENT = "cc-present"
PLAN_TOOL = "ExitPlanMode"
QUESTION_TOOL = "AskUserQuestion"
OPTION_NUMBER = re.compile(r"\s*(\d+)\b")
MACHINE_ENVELOPES = (
    "<task-notification",
    "<teammate-message",
    "<agent-message",
    "<channel ",
    "<system-reminder",
    "<local-command",
    "<command-name",
    "<command-message",
    "<user-prompt-submit-hook",
    "Caveat: The messages below",
)


@dataclass(frozen=True, slots=True)
class Touch:
    """One tool call the session tree made: what it named and what came back."""

    tool: str
    input: dict[str, Any]
    result: str
    at: datetime | None = None

    @property
    def text(self) -> str:
        return f"{json.dumps(self.input, default=str, ensure_ascii=False)}\n{self.result}"


class Sources(Protocol):
    """Where a context reads the user's words, their answers, the tree's tool calls, and the user's rulings."""

    def said(self) -> list[Evidence]: ...
    def answers(self) -> list[Answer]: ...
    def touches(self) -> list[Touch]: ...
    def rulings(self, term: str, tags: frozenset[str]) -> list[Evidence]: ...
    def ruling(self, ident: str) -> Evidence | None: ...


def tree_of(evt: BaseHookEvent) -> str:
    """The root session id of *evt*'s session tree: the spawning session for a lane, else its own."""
    return evt.ctx.root_path.stem if evt.ctx.root_path is not None else evt.session_id


def call_id(evt: BaseHookEvent) -> str:
    return evt.tool_use_id or f"{evt.session_id}/{evt.agent_id or 'main'}@{evt.ctx.event_count}"


def machine_written(text: str, markers: Sequence[str] = ()) -> bool:
    """Whether a prompt is a harness envelope or carries a tool's marker, so it is never the user's words."""
    return text.lstrip().startswith(MACHINE_ENVELOPES) or any(marker in text for marker in markers)


def squeezed(text: str) -> str:
    return " ".join(text.split())


def holds(text: str, quote: str) -> bool:
    """Whether *text* holds *quote* verbatim, whitespace folded, as whole words: ``hi`` is not in ``archives``."""
    folded = squeezed(quote)
    return bool(folded) and re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", squeezed(text)) is not None


def names(text: str, term: str) -> bool:
    """Whether *text* names *term* whole: ``term_a`` is not named by ``term_ab``, nor ``com.x`` by ``com.x.helper``."""
    return bool(term) and re.search(rf"(?<![\w.-]){re.escape(term)}(?![\w-]|\.\w)", text) is not None


def said_evidence(text: str, at: datetime | None, source: str, detail: str = "") -> Evidence:
    key = sha256(f"{at.isoformat() if at else ''}|{text}".encode()).hexdigest()[:12]
    return Evidence(id=f"words:{key}", source=source, quote=text, said_at=at, detail=detail)


def queued_words(events: Iterable[Any]) -> list[tuple[str, Any]]:
    return [
        (event.detail.prompt or "", event)
        for event in events
        if isinstance(event, AttachmentEvent)
        and isinstance(event.detail, QueuedCommand)
        and event.detail.origin == "human"
    ]


def plan_feedback(use: ToolUse) -> str:
    """What the user typed when rejecting the plan *use* submitted, without the harness banner around it.

    Only ``ExitPlanMode`` counts: the harness alone writes its result, while any other tool can return
    an error that carries the banner text.
    """
    if use.call.name != PLAN_TOOL or use.result is None or use.result.denial_kind != DENIAL_KIND_USER_REJECTED:
        return ""
    said = use.result.content.partition(USER_SAID_MARKER)[2]
    typed, trailer, _ = said.rpartition(USER_SAID_TRAILER)
    return (typed if trailer else said).strip()


def agent_before(turns: Sequence[Turn]) -> dict[str, str]:
    """Each turn's prompt mapped to the end of the agent message it answers."""
    offered: dict[str, str] = {}
    for before, turn in pairwise(turns):
        said = [
            event.text.strip() for event in before.events if isinstance(event, AssistantEvent) and event.text.strip()
        ]
        if said and turn.prompt:
            offered[turn.prompt] = said[-1][-REPLIED_SHOWN:]
    return offered


def parse_answer(payload: object) -> AskUserQuestionResult | None:
    if not isinstance(payload, dict):
        return None
    answer = parse_tool_result(QUESTION_TOOL, payload, on_error="other")
    return answer if isinstance(answer, AskUserQuestionResult) else None


def answered(use: ToolUse) -> tuple[dict[str, Any], AskUserQuestionResult] | None:
    """The question payload and the user's answer, for a finished ``AskUserQuestion`` call."""
    if use.call.name != QUESTION_TOOL or use.result is None or use.result.is_error:
        return None
    payload = use.result.tool_use_result
    if not isinstance(payload, dict) or (answer := parse_answer(payload)) is None:
        return None
    return payload, answer


def asked_options(payload: dict[str, Any], question: str) -> list[dict[str, Any]]:
    return next((q for q in payload.get("questions", []) if q.get("question") == question), {}).get("options", [])


def chosen_option(options: Sequence[dict[str, Any]], label: str) -> dict[str, Any] | None:
    """The option *label* picks: by label, or by a leading option number in a typed answer."""
    if (named := next((o for o in options if o.get("label") == label), None)) is not None:
        return named
    if (number := OPTION_NUMBER.match(label)) is not None and 1 <= int(number[1]) <= len(options):
        return options[int(number[1]) - 1]
    return None


def option_text(option: dict[str, Any]) -> str:
    preview = option.get("preview")
    return "\n".join(
        part for part in (option.get("description", ""), preview if isinstance(preview, str) else "") if part
    )


def render_answer(question: str, options: dict[str, str], chosen: Sequence[str], typed: str, notes: str) -> str:
    lines = [f"question: {question}"]
    lines += [f"option ({'chosen' if label in chosen else 'shown'}) {label}: {text}" for label, text in options.items()]
    if typed:
        lines.append(f'typed: "{typed}"')
    if notes:
        lines.append(f"notes: {notes}")
    return "\n".join(lines)


def question_answers(
    ref: str, payload: dict[str, Any], answer: AskUserQuestionResult, at: datetime | None
) -> list[Answer]:
    """One answer per question the user answered in the ``AskUserQuestion`` call *ref*."""
    found: list[Answer] = []
    for index, (question, label) in enumerate(answer.answers.items()):
        options = asked_options(payload, question)
        picked = chosen_option(options, label)
        annotation = answer.annotations.get(question)
        notes = annotation.notes if annotation is not None and annotation.notes else ""
        chosen = (picked["label"],) if picked is not None else ()
        typed = "" if picked is not None and picked.get("label") == label else label
        shown = {option.get("label", ""): option_text(option) for option in options}
        found.append(
            Answer(
                id=f"ask:{ref}#{index}",
                source="ask",
                quote="\n".join(part for part in (label, notes) if part),
                said_at=at,
                detail=render_answer(question, shown, chosen, typed, notes),
                question=question,
                options=shown,
                chosen=chosen,
                typed="\n".join(part for part in (typed, notes) if part),
            )
        )
    return found


def use_answers(use: ToolUse) -> list[Answer]:
    if (result := answered(use)) is None:
        return []
    return question_answers(use.ref.tool_use_id or f"{use.ts:%s}", *result, use.result_ts or use.ts)


def board_blocks(blocks: list[dict[str, Any]], card: str = "") -> Iterator[tuple[dict[str, Any], str]]:
    for block in blocks:
        yield block, card
        yield from board_blocks(block.get("children", []), block.get("title") or card)


def board_answer(ref: str, block: dict[str, Any], card: str, chosen: Sequence[str], typed: str) -> Answer:
    question = block.get("prompt") or block.get("title") or card
    options = {option["label"]: option.get("hint", "") for option in block.get("options", [])}
    quote = "\n".join([*chosen, *([typed] if typed else [])])
    return Answer(
        id=f"board:{ref}",
        source="board",
        quote=quote,
        detail="\n".join([f"board card: {card}", render_answer(question, options, chosen, typed, "")]),
        question=question,
        options=options,
        chosen=tuple(chosen),
        typed=typed,
    )


def board_answers(state: dict[str, Any]) -> list[Answer]:
    """The user's picks, typed inputs, feedback, and decisions on the cc-present board *state*."""
    seen = {block["id"]: (block, card) for block, card in board_blocks(state.get("doc", {}).get("blocks", []))}
    interactions = state.get("interactions", {})

    def block_of(block_id: str) -> tuple[dict[str, Any], str]:
        return seen.get(block_id, ({}, ""))

    found: list[Answer] = []
    for block_id, choice in interactions.get("choices", {}).items():
        block, card = block_of(block_id)
        chosen = [option["label"] for option in block.get("options", []) if option["id"] in choice.get("optionIds", [])]
        found.append(board_answer(f"choice:{block_id}", block, card, chosen, choice.get("other") or ""))
    for block_id, typed in interactions.get("inputs", {}).items():
        if typed.get("text"):
            found.append(board_answer(f"input:{block_id}", *block_of(block_id), (), typed["text"]))
    for block_id, notes in interactions.get("feedback", {}).items():
        found += [
            board_answer(f"feedback:{block_id}:{note['id']}", *block_of(block_id), (), note["text"]) for note in notes
        ]
    for block_id, decided in interactions.get("decisions", {}).items():
        if decided["verdict"] != "cleared":
            found.append(
                board_answer(
                    f"decision:{block_id}", *block_of(block_id), (decided["verdict"],), decided.get("note") or ""
                )
            )
    return found


def board_state(tree: str, cwd: Path | None) -> dict[str, Any]:
    argv = [PRESENT, "outcomes", "--session", tree]
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=RULINGS_TIMEOUT, env=reqenv.env_map(), check=False, cwd=cwd
        )
    except FileNotFoundError:
        return {}
    return json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else {}


def resolved(transcript: Session | RemoteSession | LazyTranscript) -> Session | RemoteSession:
    return transcript.resolve() if isinstance(transcript, LazyTranscript) else transcript


def touch_of(use: ToolUse) -> Touch:
    result = use.result.tool_use_result if use.result is not None else None
    text = (
        json.dumps(result, default=str, ensure_ascii=False)
        if isinstance(result, dict | list)
        else (use.result.content if use.result is not None else "")
    )
    raw = use.call.raw if isinstance(use.call.raw, Mapping) else {}
    return Touch(use.call.name, dict(raw), str(text), use.result_ts or use.ts)


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


def ccn_answers(evt: BaseHookEvent, term: str) -> list[dict[str, Any]]:
    argv = ["ccn", "answer", "search", term, "--json", "--limit", "0", "-R", str(evt.cwd or reqenv.cwd())]
    done = subprocess.run(
        argv, capture_output=True, text=True, timeout=RULINGS_TIMEOUT, env=reqenv.env_map(), check=False
    )
    if done.returncode != 0 or not done.stdout.strip():
        return []
    return json.loads(done.stdout)


def ccn_answer(evt: BaseHookEvent, answer_id: str) -> dict[str, Any] | None:
    argv = ["ccn", "answer", "show", answer_id, "--json", "-R", str(evt.cwd or reqenv.cwd())]
    done = subprocess.run(
        argv, capture_output=True, text=True, timeout=RULINGS_TIMEOUT, env=reqenv.env_map(), check=False
    )
    if done.returncode == CCN_NOT_FOUND:
        return None
    done.check_returncode()
    return json.loads(done.stdout)


def written_at(answer: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(answer["updated_at"])


def ruling_evidence(answer: dict[str, Any]) -> Evidence:
    return Evidence(
        id=f"ccn:{answer['id'][:7]}",
        source="ccn",
        quote=answer["body"],
        said_at=written_at(answer),
        detail=f"ruling {answer['id'][:7]}: {answer['title']}",
    )


@dataclass(frozen=True, slots=True)
class TranscriptSources:
    """The live sources: the tree's transcripts, the cc-present board, and cc-notes.

    Attributes:
        evt: The event being decided.
        needles: Text whose root-session mentions are read however far back they sit.
        window: How many recent messages of each session to read.
        machine: Markers of a prompt a tool wrote on the user's behalf; such a prompt is never their words.
    """

    evt: BaseHookEvent
    needles: tuple[str, ...]
    window: int = WINDOW
    machine: tuple[str, ...] = ()

    def sessions(self) -> list[tuple[Session | RemoteSession, bool]]:
        """The sessions to read from, each with whether its prompts are the user's."""
        ctx = self.evt.ctx
        if (root := ctx.root_transcript) is None:
            return [(ctx.t, True)]
        excerpt = ctx.root_excerpt(self.needles) if self.needles else None
        return [*([(excerpt, True)] if excerpt is not None else []), (resolved(root), True), (ctx.t, False)]

    def turns(self) -> list[tuple[Turn, bool]]:
        return [
            (turn, prompts_are_user)
            for session, prompts_are_user in self.sessions()
            for turn in session.recent_messages(self.window).turns
        ]

    def said(self) -> list[Evidence]:
        items: dict[str, Evidence] = {}
        for session, prompts_are_user in self.sessions():
            turns = session.recent_messages(self.window).turns
            offered = agent_before(turns) if prompts_are_user else {}
            for turn in turns:
                said = [(turn.prompt, turn.started_at, "prompt")] if prompts_are_user else []
                said += [(text, event.meta.timestamp, "queued") for text, event in queued_words(turn.events)]
                said += [
                    (plan_feedback(use), use.result_ts or use.ts, "plan") for use in turn.tool_uses if prompts_are_user
                ]
                for text, at, source in said:
                    if text and not machine_written(text, self.machine):
                        detail = f"the agent's message it answers ends: {offered[text]}" if text in offered else ""
                        item = said_evidence(text, at, source, detail)
                        items[item.id] = item
        return sorted(items.values(), key=lambda item: item.said_at.timestamp() if item.said_at else 0.0)

    def uses(self) -> list[ToolUse]:
        found = {use.ref.tool_use_id or f"{use.ts}": use for turn, _ in self.turns() for use in turn.tool_uses}
        return sorted(found.values(), key=lambda use: use.result_ts or use.ts)

    def answers(self) -> list[Answer]:
        asked = {item.id: item for use in self.uses() for item in use_answers(use)}
        board = {item.id: item for item in board_answers(board_state(tree_of(self.evt), self.evt.cwd))}
        return [*asked.values(), *board.values()]

    def touches(self) -> list[Touch]:
        return [touch_of(use) for use in self.uses()]

    def rulings(self, term: str, tags: frozenset[str]) -> list[Evidence]:
        cutoff = session_started(self.evt)
        return [
            ruling_evidence(answer)
            for answer in ccn_answers(self.evt, term)
            if names(answer.get("body", ""), term)
            and written_at(answer) < cutoff
            and tags <= set(answer.get("tags", ()))
        ]

    def ruling(self, ident: str) -> Evidence | None:
        answer = ccn_answer(self.evt, ident)
        return (
            ruling_evidence(answer) if answer is not None and written_at(answer) < session_started(self.evt) else None
        )


@dataclass(frozen=True, slots=True)
class FakeSources:
    """Sources a test declares outright."""

    said_items: tuple[Evidence, ...] = ()
    answer_items: tuple[Answer, ...] = ()
    touch_items: tuple[Touch, ...] = ()
    ruling_items: tuple[Evidence, ...] = ()

    def said(self) -> list[Evidence]:
        return list(self.said_items)

    def answers(self) -> list[Answer]:
        return list(self.answer_items)

    def touches(self) -> list[Touch]:
        return list(self.touch_items)

    def rulings(self, term: str, tags: frozenset[str]) -> list[Evidence]:
        return [item for item in self.ruling_items if names(item.quote, term)]

    def ruling(self, ident: str) -> Evidence | None:
        return next((item for item in self.ruling_items if item.id == f"ccn:{ident[:7]}"), None)


@dataclass(frozen=True)
class Context:
    """The action a declaration decides, and everything the session tree knows that bears on it.

    Attributes:
        kind: The declaration's kind.
        action: The action to cover.
        tree: The root session id of the acting tree.
        trees: The trees whose tickets cover the action: its own, and its Orca coordinator's.
        session: The acting session.
        agent: The acting agent.
        call: The id of the pending call.
        sources: Where the user's words, answers, the tree's tool calls, and the user's rulings are read.
        evt: The event being decided; ``None`` for a context a test builds.
        facts: Lookups a hook injects by type, such as a thread read from a chat system.
    """

    kind: str
    action: Proposal
    tree: str
    trees: tuple[str, ...]
    session: str
    agent: str
    call: str
    sources: Sources
    evt: BaseHookEvent | None = None
    facts: dict[type, Any] = field(default_factory=dict[type, Any])

    @classmethod
    def of(
        cls,
        evt: BaseHookEvent,
        kind: str,
        action: Proposal,
        *,
        trees: Sequence[str] = (),
        window: int = WINDOW,
        machine: Sequence[str] = (),
    ) -> Context:
        """The live context of *evt*, remembered on the event so conditions reach it through :meth:`current`."""
        tree = tree_of(evt)
        needles = tuple(value for value in action.place.values() if value)
        ctx = cls(
            kind=kind,
            action=action,
            tree=tree,
            trees=tuple(dict.fromkeys((tree, *trees))),
            session=evt.session_id,
            agent=evt.agent_id or "main",
            call=call_id(evt),
            sources=TranscriptSources(evt, needles, window, tuple(machine)),
            evt=evt,
        )
        evt.__dict__["_ticket_context"] = ctx
        return ctx

    @classmethod
    def current(cls, evt: BaseHookEvent) -> Context:
        """The context the declaration deciding *evt* built."""
        return evt.__dict__["_ticket_context"]

    @classmethod
    def fake(
        cls,
        kind: str,
        place: dict[str, str],
        *,
        payload: dict[str, Any] | None = None,
        summary: str = "",
        said: Sequence[Evidence | str] = (),
        answers: Sequence[Answer] = (),
        touches: Sequence[Touch] = (),
        rulings: Sequence[Evidence] = (),
        facts: Sequence[Any] = (),
        tree: str = "root-session",
        session: str = "root-session",
        agent: str = "main",
        call: str = "toolu_1",
    ) -> Context:
        """A context a test declares outright; a plain string in *said* is one prompt of the user's."""
        words = tuple(said_evidence(item, None, "prompt") if isinstance(item, str) else item for item in said)
        return cls(
            kind=kind,
            action=Proposal(place, payload or {}, summary),
            tree=tree,
            trees=(tree,),
            session=session,
            agent=agent,
            call=call,
            sources=FakeSources(words, tuple(answers), tuple(touches), tuple(rulings)),
            facts={type(fact): fact for fact in facts},
        )

    @property
    def place(self) -> dict[str, str]:
        return dict(self.action.place)

    @cached_property
    def said(self) -> tuple[Evidence, ...]:
        """The user's words across the tree, oldest first."""
        return tuple(self.sources.said())

    @cached_property
    def answers(self) -> tuple[Answer, ...]:
        """The user's answers to the agent's questions, on ``AskUserQuestion`` and the board."""
        return tuple(self.sources.answers())

    @cached_property
    def touches(self) -> tuple[Touch, ...]:
        """The tree's tool calls, oldest first."""
        return tuple(self.sources.touches())

    def rulings(self, term: str, tags: frozenset[str] = frozenset()) -> list[Evidence]:
        """The user's durable rulings naming *term*, recorded as cc-notes answers before the session started."""
        return self.sources.rulings(term, tags)

    def ruling(self, ident: str) -> Evidence | None:
        """The user's standing ruling *ident*, when it was recorded before the session started."""
        return self.sources.ruling(ident)

    def fact[F](self, kind: type[F], default: Callable[[Context], F]) -> F:
        """The injected lookup of type *kind*, or *default* of this context, built once."""
        if kind not in self.facts:
            self.facts[kind] = default(self)
        return self.facts[kind]
