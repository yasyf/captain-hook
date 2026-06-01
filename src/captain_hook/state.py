"""Hook fire-count and primitive echo-suppression state, plus shared NLP resources (spaCy, WordNet)."""
from __future__ import annotations

import inspect
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from types import FrameType, ModuleType

    import spacy

    from captain_hook.events import BaseHookEvent
    from captain_hook.types import Signals

FRAMEWORK_DIR = str(Path(__file__).resolve().parent)
CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "captain-hook"
SPACY_MODEL = "en_core_web_sm"


class NlpResources:
    @cached_property
    def spacy(self) -> spacy.language.Language:
        import spacy

        from captain_hook.util.model_cache import MODEL_NAME, MODEL_VERSION, cache_root

        if spacy.util.is_package(SPACY_MODEL):
            return spacy.load(SPACY_MODEL)
        # We refuse to auto-download from a live hook: it's a ~100MB silent fetch behind
        # the agent's back. If a previous run / explicit install already populated the
        # cache, use that; otherwise, raise with an actionable install hint.
        cached = cache_root() / f"{MODEL_NAME}-{MODEL_VERSION}" / MODEL_NAME / f"{MODEL_NAME}-{MODEL_VERSION}"
        if cached.is_dir():
            return spacy.load(cached)
        raise RuntimeError(
            f"spaCy model {SPACY_MODEL!r} is not installed. "
            f"Install it explicitly before running hooks that use NLP signals: "
            f"`python -m spacy download {SPACY_MODEL}` "
            f"or `python -c \"from captain_hook.util.model_cache import ensure_spacy_model; ensure_spacy_model()\"`."
        )

    @cached_property
    def wn(self) -> ModuleType:
        import wn

        if not wn.lexicons(lexicon="oewn:2025"):
            wn.download("oewn:2025", progress_handler=None)
        return wn


RESOURCES = NlpResources()


class HookState(BaseModel):
    """Per-hook persistent state tracked across events in a session (currently just ``fire_count`` for ``max_fires``)."""

    fire_count: int = 0


# ECHO_WINDOW: number of subsequent transcript messages after a nudge fires during which we
# suppress restating the same idea. Tuned for "the agent reads our nudge and the next ~5
# assistant messages reference the same concept".
# ECHO_THRESHOLD: fraction of content lemmas in a candidate text that must overlap with the
# nudge's lemmas to count as an echo. 0.4 = "if 40%+ of the meaningful words match, the
# agent is parroting".
# ECHO_MIN_OVERLAP: absolute minimum overlap to count, so short messages don't pass the
# fractional threshold trivially (e.g. a 2-token message would otherwise hit 0.5).
ECHO_WINDOW = 5
ECHO_THRESHOLD = 0.4
ECHO_MIN_OVERLAP = 2


class PrimitiveState(BaseModel):
    """Per-primitive state for nudges/gates: last fire index, consumed-signal hashes, and echo-window lemmas."""

    last_fired_at: int = 0
    consumed: set[str] = Field(default_factory=set)
    echo_lemmas: set[str] = Field(default_factory=set)
    echo_window_end: int = 0

    @staticmethod
    def content_lemmas(text: str) -> set[str]:
        return {
            tok.lemma_.lower()
            for tok in RESOURCES.spacy(text)
            if tok.pos_ in {"NOUN", "VERB", "ADJ"} and not tok.is_stop and len(tok.lemma_) > 2
        }

    def is_echo(self, text: str) -> bool:
        return bool(
            self.echo_lemmas
            and (text_lemmas := self.content_lemmas(text))
            and len(overlap := text_lemmas & self.echo_lemmas) >= ECHO_MIN_OVERLAP
            and len(overlap) / len(text_lemmas) >= ECHO_THRESHOLD
        )

    def consume_echoes(self, texts: list[str], transcript_len: int) -> None:
        if not self.echo_lemmas or transcript_len >= self.echo_window_end:
            return

        for text in texts:
            if (h := text_hash(text)) not in self.consumed and self.is_echo(text):
                self.consumed.add(h)

    def seed_echo_window(self, triggering_texts: list[str], message: str, transcript_len: int) -> None:
        self.echo_lemmas = self.content_lemmas(" ".join(triggering_texts)) | self.content_lemmas(message)
        self.echo_window_end = transcript_len + ECHO_WINDOW

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
    suffix = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") if label else sha256(message.encode()).hexdigest()[:8]
    return f"{caller_stem()}:{prefix}_{suffix}"


def record_fire(evt: BaseHookEvent) -> None:
    ps = evt.ctx.s[PrimitiveState].get(PrimitiveState())
    ps.last_fired_at = len(evt.ctx.t)
    evt.ctx.s[PrimitiveState].set(ps)


def fired_this_turn(evt: BaseHookEvent) -> bool:
    return (ps := evt.ctx.s[PrimitiveState].get()) is not None and ps.last_fired_at > evt.ctx.turn.start_idx


from captain_hook.session import SessionStore  # noqa: E402


@dataclass
class EchoTracker:
    window: int = ECHO_WINDOW
    threshold: float = ECHO_THRESHOLD
    min_overlap: int = ECHO_MIN_OVERLAP

    def saw(self, text: str, *, evt: BaseHookEvent) -> bool:
        ps = evt.ctx.s[PrimitiveState].get()
        return (
            ps is not None
            and bool(ps.echo_lemmas)
            and len(evt.ctx.t) < ps.echo_window_end
            and ps.is_echo(text)
        )

    def record(self, text: str, triggering: Iterable[str], *, evt: BaseHookEvent) -> None:
        ps = evt.ctx.s[PrimitiveState].get(PrimitiveState())
        ps.echo_lemmas = PrimitiveState.content_lemmas(" ".join(triggering)) | PrimitiveState.content_lemmas(text)
        ps.echo_window_end = len(evt.ctx.t) + self.window
        evt.ctx.s[PrimitiveState].set(ps)


T = TypeVar("T", bound=BaseModel)


def workflow_state(name: str) -> Callable[[type[T]], type[T]]:
    def wrap(cls: type[T]) -> type[T]:
        cls.__workflow_name__ = name  # type: ignore[attr-defined]
        SessionStore.track(cls)

        def load(inner_cls: type[T], evt: BaseHookEvent) -> T:
            return evt.ctx.s.load(inner_cls)

        def save(self: T, evt: BaseHookEvent) -> None:
            evt.ctx.s[type(self)].set(self)

        def reset(inner_cls: type[T], evt: BaseHookEvent) -> None:
            evt.ctx.s[inner_cls].delete()

        cls.load = classmethod(load)  # type: ignore[attr-defined]
        cls.save = save  # type: ignore[attr-defined]
        cls.reset = classmethod(reset)  # type: ignore[attr-defined]
        return cls

    return wrap


SessionStore.track(HookState)
SessionStore.track(PrimitiveState)
