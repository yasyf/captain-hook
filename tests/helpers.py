from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from captain_hook.context import HookContext
from captain_hook.session import SessionStore
from captain_hook.transcript import Transcript
from captain_hook.transcript.models import TextBlock, TranscriptMessage


def make_ctx(
    session_dir: Path | None = None,
    *,
    texts: list[str] | None = None,
    n_messages: int = 10,
    settings: Any = None,
    call_llm_return: Any = None,
) -> HookContext:
    msgs = [TranscriptMessage(type="assistant", content=[TextBlock(text=t)]) for t in (texts or [])]
    while len(msgs) < n_messages:
        msgs.insert(0, TranscriptMessage(type="assistant", content=[]))
    ctx = HookContext(
        session=SessionStore(session_dir),
        transcript=Transcript(messages=msgs),
        settings=settings,
    )
    ctx.call_llm = MagicMock(return_value=call_llm_return)  # type: ignore[method-assign]
    return ctx
