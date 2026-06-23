from __future__ import annotations

import itertools
import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from cc_transcript import parse_events_from_bytes

from captain_hook.review.judge import ReviewVerdict
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


def parse(entries: list[dict[str, Any]]) -> list[TranscriptEvent]:
    return parse_events_from_bytes("".join(json.dumps(entry) + "\n" for entry in entries).encode())


def write_transcript(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def _llm_backend_available() -> bool:
    from cc_transcript.judge.llm import default_backend
    from spawnllm import BackendUnavailable

    try:
        default_backend()
    except BackendUnavailable:
        return False
    return True


requires_llm_backend = pytest.mark.skipif(
    not _llm_backend_available(),
    reason="no installed, authenticated LLM backend (spawnllm.select_backend raised BackendUnavailable)",
)


def install_judge(
    monkeypatch: pytest.MonkeyPatch, *, category: Category = "durable_style_rule", fail_on: str | None = None
) -> list[str]:
    calls: list[str] = []

    async def judge(prompt: str) -> ReviewVerdict:
        calls.append(prompt)
        if fail_on is not None and fail_on in prompt:
            raise subprocess.CalledProcessError(1, ["claude"])
        return ReviewVerdict(category=category, summary="states a durable rule", confidence=0.9, rationale="r")

    monkeypatch.setattr("captain_hook.review.judge.structured_judge", lambda *_, **__: judge)
    return calls
