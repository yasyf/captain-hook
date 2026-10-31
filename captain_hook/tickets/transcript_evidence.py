from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from captain_hook.snapshots.client import (
    CURRENT_CLIENT,
    HOOK_TAIL_BYTES,
    NATIVE_CLASSIFIER,
    EvidenceIncomplete,
    FixedLease,
    RemoteSession,
    SnapshotProtocolError,
)
from captain_hook.transcripts import LazyTranscript, TranscriptSource
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from cc_transcript.activity import ToolUse
    from cc_transcript.query import Session

    from captain_hook.tickets.context import TranscriptSources


@dataclass(frozen=True, slots=True)
class EvidenceSession:
    session: Session | RemoteSession
    prompts_are_user: bool
    indexed: bool = False


@dataclass(frozen=True, slots=True)
class InstructionTurn:
    prompt: str
    started_at: datetime | None
    queued: tuple[tuple[str, datetime], ...]
    rejections: tuple[tuple[str, str, datetime], ...]
    assistant_tail: str


def instruction_turns(source: EvidenceSession, window: int) -> list[InstructionTurn]:
    from cc_transcript.filterspec import DENIAL_KIND_USER_REJECTED
    from cc_transcript.models import AssistantEvent

    from captain_hook.tickets.context import REPLIED_SHOWN, queued_words

    session = source.session.recent_messages(window)
    if source.indexed:
        return [
            InstructionTurn(
                row["prompt"], row["started_at"],
                tuple((item["text"], item["ts"]) for item in row["queued"]),
                tuple((item["name"], item["content"], item["result_ts"]) for item in row["rejections"]),
                row["assistant_tail"],
            )
            for row in session.query({"kind": "instruction_turns", "order": "forward"})
        ]
    return [
        InstructionTurn(
            turn.prompt, turn.started_at,
            tuple((text, event.meta.timestamp) for text, event in queued_words(turn.events)),
            tuple(
                (use.call.name, use.result.content, use.result_ts or use.ts)
                for use in turn.tool_uses
                if use.result is not None and use.result.denial_kind == DENIAL_KIND_USER_REJECTED
            ),
            next((
                event.text.strip()[-REPLIED_SHOWN:]
                for event in reversed(turn.events)
                if isinstance(event, AssistantEvent) and event.text.strip()
            ), ""),
        )
        for turn in session.turns
    ]


def fixed_session(client: Any, description: dict[str, Any], stack: ExitStack) -> RemoteSession:
    session = RemoteSession(
        client, FixedLease(client, description, tail_bytes=HOOK_TAIL_BYTES),
        Path(description["canonical_path"]), description["classifier"], tail_bytes=HOOK_TAIL_BYTES,
    )
    stack.callback(session.release)
    return session


def acquired_source(client: Any, spec: TranscriptSource, stack: ExitStack) -> Session | RemoteSession:
    from captain_hook.transcripts import load_transcript

    try:
        description = client.acquired(spec.path, classifier=NATIVE_CLASSIFIER, tail_bytes=HOOK_TAIL_BYTES)
    except EvidenceIncomplete as error:
        if spec.unborn is not None and spec.unborn.is_set() and error.status == "missing":
            return load_transcript(None)
        raise
    if spec.unborn is not None:
        spec.unborn.clear()
    return fixed_session(client, description, stack)


def classified(session: RemoteSession, stack: ExitStack) -> RemoteSession:
    from captain_hook.app import _state
    from captain_hook.transcripts import configured_classifier_policy, resolve_project_dir

    client = session.client
    if _state.classifier is not None:
        result = client.classify(session, _state.classifier, configured_classifier_policy(resolve_project_dir()))
        stack.callback(result.release)
        return result
    prepared = list(client.pages(
        "prepare_hook_view", domain=True, view=session.view(), cwd=resolve_project_dir(),
        droid=reqenv.getenv("FACTORY_PROJECT_DIR") is not None,
    ))
    if len(prepared) != 1 or prepared[0].get("kind") != "classifier":
        raise SnapshotProtocolError("hook classifier preparation returned invalid evidence")
    classifier = prepared[0]["classifier"]
    if classifier == session.classifier:
        return session
    acquired = list(client.pages("classify_view", view=session.view(), classifier=classifier))
    if len(acquired) != 1 or acquired[0].get("kind") != "acquired":
        raise SnapshotProtocolError("view classification returned no leased description")
    return fixed_session(client, acquired[0]["description"], stack)


def source_spec(transcript: Any, stack: ExitStack) -> TranscriptSource | None:
    if type(transcript) is not LazyTranscript:
        return None
    branch = transcript.fork()
    stack.callback(branch.release)
    return branch.pins.spec


@contextmanager
def sessions(sources: TranscriptSources) -> Iterator[list[EvidenceSession]]:
    with ExitStack() as stack:
        ctx = sources.evt.ctx
        acting = source_spec(ctx.transcript, stack)
        root = source_spec(ctx.root_transcript, stack) if ctx.root_transcript is not None else None
        if acting is None or acting.tail_events is not None or (ctx.root_transcript is not None and root is None):
            yield [EvidenceSession(session, user) for session, user in sources.sessions()]
            return
        client = CURRENT_CLIENT.get()
        if client is None:
            raise EvidenceIncomplete("invalid_request", "ticket evidence requires an admitted snapshot client")
        output = []
        if root is not None:
            base = fixed_session(client, client.acquired(root.path, classifier=NATIVE_CLASSIFIER, tail_bytes=HOOK_TAIL_BYTES), stack)
            forms = list(dict.fromkeys(
                form for needle in sources.needles if needle
                for form in (needle, json.dumps(needle)[1:-1], json.dumps(needle, ensure_ascii=False)[1:-1])
            ))
            if sources.needles or root.tail_events is not None:
                result = list(client.pages(
                    "acquire_views", handle=base.lease.require(),
                    excerpt={"forms": forms, "around": 2, "count": 20} if sources.needles else None,
                    tail_events=root.tail_events,
                ))
                if len(result) != 1 or result[0].get("kind") != "acquired_views":
                    raise SnapshotProtocolError("ticket source selection returned no views")
                expected = (["excerpt"] if sources.needles else []) + (["tail"] if root.tail_events else [])
                views = result[0]["views"]
                acquired = [fixed_session(client, view["description"], stack) for view in views]
                if [view["label"] for view in views] != expected:
                    raise SnapshotProtocolError("ticket source selection returned different views")
                output.extend(EvidenceSession(classified(session, stack), True, True) for session in acquired)
            if root.tail_events is None:
                output.append(EvidenceSession(classified(base, stack), True, True))
        base = acquired_source(client, acting, stack)
        output.append(EvidenceSession(classified(base, stack), root is None, True) if isinstance(base, RemoteSession) else EvidenceSession(base, root is None))
        yield output


def batches(values: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), 256):
        yield values[start:start + 256]


def question_uses(sources: Sequence[EvidenceSession]) -> list[ToolUse]:
    from captain_hook.tickets.context import QUESTION_TOOL

    candidates: dict[str, None] = {}
    legacy: dict[int, dict[str, ToolUse]] = {}
    for index, source in enumerate(sources):
        if source.indexed:
            for record in source.session.query({"kind": "interaction_keys", "name": QUESTION_TOOL}):
                candidates[record["key"]] = None
        else:
            found = {use.ref.tool_use_id or f"{use.ts}": use for turn in source.session.turns for use in turn.tool_uses}
            legacy[index] = found
            for key, use in found.items():
                if use.call.name == QUESTION_TOOL:
                    candidates[key] = None
    keys = list(candidates)
    first: dict[str, tuple[int, int]] = {}
    winners: dict[str, tuple[int, str]] = {}
    for index, source in enumerate(sources):
        if source.indexed:
            records = [record for batch in batches(keys) for record in source.session.query({"kind": "interaction_winners", "keys": list(batch)})]
            if len(records) != len(keys) or [record["key"] for record in records] != keys:
                raise SnapshotProtocolError("interaction winners returned different keys")
            for record in records:
                if record["present"]:
                    first.setdefault(record["key"], (index, record["first"]))
                    winners[record["key"]] = index, record["name"]
        else:
            for rank, (key, use) in enumerate(legacy[index].items()):
                if key in candidates:
                    first.setdefault(key, (index, rank))
                    winners[key] = index, use.call.name
    selected: dict[str, ToolUse] = {}
    for index, source in enumerate(sources):
        wanted = [key for key in keys if winners.get(key) == (index, QUESTION_TOOL)]
        if source.indexed:
            found = [use for batch in batches(wanted) for use in source.session.query({"kind": "interaction_records", "keys": list(batch)})]
            actual = [use.ref.tool_use_id or f"{use.ts}" for use in found]
            if actual != wanted:
                raise SnapshotProtocolError("interaction records returned different keys")
            selected.update(zip(actual, found, strict=True))
        else:
            selected.update((key, legacy[index][key]) for key in wanted)
    return sorted(selected.values(), key=lambda use: (use.result_ts or use.ts, first[use.ref.tool_use_id or f"{use.ts}"]))


def card_taps(source: EvidenceSession) -> Iterator[tuple[dict[str, Any], Any]]:
    from captain_hook.tickets.context import card_payload, card_taps as legacy_card_taps

    if not source.indexed:
        for turn in source.session.turns:
            yield from legacy_card_taps(turn.events)
        return
    for record in source.session.query({"kind": "meta_messages", "contains": "slack.action"}):
        trusted = (
            record["kind"] == "user" and record["is_meta"]
            and record["source_tool_use_id"] is None and record["source_tool_assistant_uuid"] is None
        ) or (record["kind"] == "queued" and record["origin"] == "channel")
        if trusted and (payload := card_payload(record["text"])) is not None:
            yield payload, record["ts"]
