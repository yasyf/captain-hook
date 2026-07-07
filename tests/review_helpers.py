from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from cc_transcript import parse_events_from_bytes
from cc_transcript.filterspec import ANSWERED_PREFIX, ANSWERED_TRAILER
from cc_transcript.judge import JudgeError, canonical_slug
from cc_transcript.mining.signals import NO_OPTION_SELECTED

from captain_hook.review.judge import DURABLE_CATEGORIES, ReviewVerdict
from captain_hook.review.repo import RepoKey

if TYPE_CHECKING:
    from pathlib import Path

    from cc_transcript.models import TranscriptEvent

    from captain_hook.review.judge import Category

REPO = RepoKey("github.com/yasyf/captain-hook")
BASE_TS = "2026-06-01T12:00:00+00:00"
CORRECTION = "no, never use a bare except here, always catch the specific parser error"
PROMPT_VERSION = 1

REVIEW_UUIDS = itertools.count()


@dataclass(frozen=True, slots=True)
class Verdict:
    accepted: bool = True
    confidence: float = 0.9
    category: str = "durable_correction"
    summary: str = "user corrected approach"
    rationale: str = "explicit correction"
    canonical_key: str | None = None


def next_uuid() -> str:
    return f"uuid-{next(REVIEW_UUIDS)}"


def envelope(entry_type: str, **overrides: Any) -> dict[str, Any]:
    return {
        "type": entry_type,
        "uuid": overrides.pop("uuid", next_uuid()),
        "parentUuid": None,
        "sessionId": overrides.pop("sessionId", "sess-1"),
        "timestamp": overrides.pop("timestamp", BASE_TS),
        "cwd": overrides.pop("cwd", "/repo"),
        "gitBranch": "main",
        "version": "1.2.3",
        "isSidechain": False,
        "isMeta": False,
        "entrypoint": "cli",
        **overrides,
    }


def user_text(text: str, **overrides: Any) -> dict[str, Any]:
    return envelope("user", message={"role": "user", "content": text}, **overrides)


def assistant_text(text: str, **overrides: Any) -> dict[str, Any]:
    return envelope(
        "assistant",
        message={"role": "assistant", "model": "claude", "content": [{"type": "text", "text": text}]},
        **overrides,
    )


def assistant_tool_use(tool_id: str, name: str, tool_input: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return envelope(
        "assistant",
        message={
            "role": "assistant",
            "model": "claude",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
        },
        **overrides,
    )


def tool_result(tool_id: str, content: str, *, is_error: bool = False, **overrides: Any) -> dict[str, Any]:
    return envelope(
        "user",
        message={
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}],
        },
        **overrides,
    )


def correction_entries(*, session: str = "sess-1", timestamp: str = BASE_TS, **overrides: Any) -> list[dict[str, Any]]:
    return [
        assistant_text("I'll wrap the parser in a bare except", sessionId=session, timestamp=timestamp, **overrides),
        user_text(CORRECTION, sessionId=session, timestamp=timestamp, **overrides),
    ]


def ask_user_question_round(
    question: str,
    *,
    notes: str | None = None,
    selected: str | None = None,
    options: tuple[str, ...] = (),
    header: str = "Choice",
    multi_select: bool = False,
    tool_id: str = "q1",
    session: str = "sess-1",
    timestamp: str = BASE_TS,
) -> list[dict[str, Any]]:
    value = f'"{selected}"' if selected is not None else NO_OPTION_SELECTED
    body = f'"{question}"={value}' + (f" notes: {notes}" if notes is not None else "")
    return [
        assistant_tool_use(
            tool_id,
            "AskUserQuestion",
            {
                "questions": [
                    {
                        "question": question,
                        "header": header,
                        "multiSelect": multi_select,
                        "options": [{"label": label} for label in options],
                    }
                ]
            },
            sessionId=session,
            timestamp=timestamp,
        ),
        tool_result(tool_id, ANSWERED_PREFIX + body + ANSWERED_TRAILER, sessionId=session, timestamp=timestamp),
    ]


def parse(entries: list[dict[str, Any]]) -> list[TranscriptEvent]:
    return parse_events_from_bytes("".join(json.dumps(entry) + "\n" for entry in entries).encode())


def write_transcript(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def llm_backend_available() -> bool:
    from cc_transcript.judge.llm import default_backend
    from spawnllm import BackendUnavailable

    try:
        default_backend()
    except BackendUnavailable:
        return False
    return True


requires_llm_backend = pytest.mark.skipif(
    not llm_backend_available(),
    reason="no installed, authenticated LLM backend (spawnllm.select_backend raised BackendUnavailable)",
)


def default_slug_for(text: str) -> str:
    return canonical_slug(" ".join(text.split()[:4]))


def default_slug(prompt: str) -> str:
    return default_slug_for(prompt.split("=== FEEDBACK TO CLASSIFY ===\n", 1)[-1])


def install_judge(
    monkeypatch: pytest.MonkeyPatch,
    *,
    category: Category = "durable_style_rule",
    fail_on: str | None = None,
    slug: str | None = None,
) -> list[str]:
    calls: list[str] = []

    async def judge(prompt: str) -> ReviewVerdict:
        calls.append(prompt)
        if fail_on is not None and fail_on in prompt:
            raise JudgeError("claude exited 1")
        rule_slug = slug if slug is not None else (default_slug(prompt) if category in DURABLE_CATEGORIES else None)
        return ReviewVerdict(
            category=category, summary="states a durable rule", confidence=0.9, rationale="r", rule_slug=rule_slug
        )

    monkeypatch.setattr("captain_hook.review.judge.structured_judge", lambda *_, **__: judge)
    return calls


def install_fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swaps the ``[judge]`` static embedder for a deterministic numpy stand-in.

    Patches the loader the whole ``cc_transcript.judge.similar`` surface reads —
    :func:`~cc_transcript.judge.verdicts.VerdictStoreMixin.record_verdict`'s
    evidence embed and ``suggest_canonical_keys``'s query embed — so slug
    suggestions round-trip through the real sqlite-vec store without downloading
    ``potion-retrieval-32M``. Each text maps to a stable L2-normalized vector.
    """
    import numpy as np
    from cc_transcript.judge.similar import EMBED_DIM

    def embed(text: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")
        vector = np.random.default_rng(seed).standard_normal(EMBED_DIM).astype(np.float32)
        return vector / np.linalg.norm(vector)

    monkeypatch.setattr("cc_transcript.judge.similar.default_embedder", lambda: embed)
