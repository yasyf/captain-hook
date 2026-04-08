from __future__ import annotations

import inspect
import re
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from types import FrameType

    import spacy

    from captain_hook.events import BaseHookEvent
    from captain_hook.types import Signals

FRAMEWORK_DIR = str(Path(__file__).resolve().parent)

NLP: spacy.language.Language | None = None


def get_nlp() -> spacy.language.Language:
    global NLP  # noqa: PLW0603
    if NLP is None:
        import spacy as _spacy

        NLP = _spacy.load("en_core_web_sm")  # pyright: ignore[reportConstantRedefinition]
    return NLP


class HookState(BaseModel):
    """Per-hook session state tracking the number of times a hook has fired."""

    fire_count: int = 0


class PrimitiveState(BaseModel):
    """Session state for nudge/gate primitives: signal deduplication and echo suppression.

    Tracks consumed content hashes to prevent re-firing on the same text,
    and maintains echo lemmas to suppress semantically similar messages
    within a window.
    """

    ECHO_WINDOW: int = 5
    ECHO_THRESHOLD: float = 0.4
    ECHO_MIN_OVERLAP: int = 2

    last_fired_at: int = 0
    consumed: set[str] = Field(default_factory=set)
    echo_lemmas: set[str] = Field(default_factory=set)
    echo_window_end: int = 0

    @staticmethod
    def content_lemmas(text: str) -> set[str]:
        return {
            tok.lemma_.lower()
            for tok in get_nlp()(text)
            if tok.pos_ in {"NOUN", "VERB", "ADJ"} and not tok.is_stop and len(tok.lemma_) > 2
        }

    def is_echo(self, text: str) -> bool:
        return bool(
            self.echo_lemmas
            and (text_lemmas := self.content_lemmas(text))
            and len(overlap := text_lemmas & self.echo_lemmas) >= self.ECHO_MIN_OVERLAP
            and len(overlap) / len(text_lemmas) >= self.ECHO_THRESHOLD
        )

    def consume_echoes(self, texts: list[str], transcript_len: int) -> None:
        if not self.echo_lemmas or transcript_len >= self.echo_window_end:
            return
        for text in texts:
            if (h := text_hash(text)) not in self.consumed and self.is_echo(text):
                self.consumed.add(h)

    def seed_echo_window(self, triggering_texts: list[str], message: str, transcript_len: int) -> None:
        self.echo_lemmas = self.content_lemmas(" ".join(triggering_texts)) | self.content_lemmas(message)
        self.echo_window_end = transcript_len + self.ECHO_WINDOW

    def match_signals(self, sig: Signals, texts: list[str]) -> list[str] | None:
        from captain_hook.signals import score_signals

        contributing_hashes = [
            h
            for text in texts
            if (h := text_hash(text)) not in self.consumed and score_signals(sig.patterns, text) >= sig.threshold
        ]
        if not contributing_hashes:
            return None
        self.consumed.update(contributing_hashes)
        return [t for t in texts if text_hash(t) in set(contributing_hashes)]


def text_hash(text: str) -> str:
    """Return a 16-char hex SHA-256 hash of text for content deduplication.

    Args:
        text: The text to hash.

    Returns:
        First 16 characters of the SHA-256 hex digest.
    """
    return sha256(text.encode()).hexdigest()[:16]


def package_aware_stem(p: Path) -> str:
    if (
        p.name != "__init__.py"
        and not str(p).startswith(FRAMEWORK_DIR)
        and (init := p.parent / "__init__.py").exists()
        and init.stat().st_size > 0
    ):
        return f"{p.parent.name}.{p.stem}"
    return p.stem


def caller_stem() -> str:
    frame: FrameType | None = inspect.currentframe()
    if frame:
        frame = frame.f_back
    while frame and frame.f_code.co_filename.startswith(FRAMEWORK_DIR):
        frame = frame.f_back
    return package_aware_stem(Path(frame.f_code.co_filename)) if frame else "unknown"


def hook_name(prefix: str, label: str | None, message: str) -> str:
    """Generate a deterministic hook name from the caller's module, prefix, and label/message.

    Args:
        prefix: Hook type prefix (e.g. ``"nudge"``, ``"gate"``, ``"lint"``).
        label: Human-readable label, or None to use a message hash.
        message: Fallback text hashed when no label is provided.

    Returns:
        Name in the form ``"module:prefix_suffix"``.
    """
    suffix = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") if label else sha256(message.encode()).hexdigest()[:8]
    return f"{caller_stem()}:{prefix}_{suffix}"


def record_fire(evt: BaseHookEvent) -> None:
    """Record that a primitive fired during this event, updating ``PrimitiveState.last_fired_at``."""
    ps = evt.ctx.s[PrimitiveState].get() or PrimitiveState()
    ps.last_fired_at = len(evt.ctx.t)
    evt.ctx.s[PrimitiveState].set(ps)


def fired_this_turn(evt: BaseHookEvent) -> bool:
    """Check whether a primitive already fired during the current turn.

    Args:
        evt: The current hook event.

    Returns:
        True if ``PrimitiveState.last_fired_at`` exceeds the current turn's start index.
    """
    return (ps := evt.ctx.s[PrimitiveState].get()) is not None and ps.last_fired_at > evt.ctx.turn.start_idx
