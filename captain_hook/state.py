"""Hook fire-count and primitive echo-suppression state, plus shared NLP resources (spaCy, WordNet)."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, TypeVar

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from types import FrameType, ModuleType

    import spacy

    from captain_hook.events import BaseHookEvent
    from captain_hook.types import Signals

FRAMEWORK_DIR = str(Path(__file__).resolve().parent)
PACKS_DIR = str(Path(FRAMEWORK_DIR) / "packs")
SPACY_MODEL = "en_core_web_sm"


class NlpResources:
    @cached_property
    def spacy(self) -> spacy.language.Language:
        import spacy

        from captain_hook.util.model_cache import cached_pipeline

        if spacy.util.is_package(SPACY_MODEL):
            return spacy.load(SPACY_MODEL)
        # We refuse to auto-download from a live hook: it's a silent fetch behind the
        # agent's back (~13MB for spaCy; the oewn lexicon is the ~231MB heavyweight).
        # If a previous run / explicit install already populated the cache, use that;
        # otherwise, raise with an actionable install hint.
        if cached := cached_pipeline():
            return spacy.load(cached)
        raise RuntimeError(
            f"spaCy model {SPACY_MODEL!r} is not installed. "
            "Run `uvx capt-hook register-hooks` to provision NLP resources, or install the model "
            f"explicitly: `python -m spacy download {SPACY_MODEL}` "
            f'or `python -c "from captain_hook.util.model_cache import ensure_spacy_model; ensure_spacy_model()"`.'
        )

    @cached_property
    def wn(self) -> ModuleType:
        import wn

        from captain_hook.util.model_cache import ensure_wn_lexicon

        ensure_wn_lexicon()
        return wn


RESOURCES = NlpResources()


class HookState(BaseModel):
    """Per-hook persistent state tracked across events in a session (``fire_count`` for ``max_fires``)."""

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
    """Per-primitive nudge/gate state shared across all hooks in a session.

    ``last_fired_at`` (the session-global turn-throttle read by every LLM hook and
    blocking gate) and the ``echo_lemmas``/``echo_window_end`` window (a global
    last-write-wins "don't re-trigger on a parrot of the last nudge" filter) are
    deliberately session-scoped, not per hook. ``consumed`` is a per-hook-name ledger
    of signal-text hashes each hook has already scored, so one hook's aggregate fire
    cannot mute another's signals — access it through :meth:`consumed_for`.
    """

    last_fired_at: int = 0
    consumed: dict[str, set[str]] = Field(default_factory=dict)
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

    def seed_echo_window(self, triggering_texts: list[str], message: str, transcript_len: int) -> None:
        self.echo_lemmas = self.content_lemmas(" ".join(triggering_texts)) | self.content_lemmas(message)
        self.echo_window_end = transcript_len + ECHO_WINDOW

    def consumed_for(self, hook: str) -> set[str]:
        """The mutable set of signal-text hashes ``hook`` has already consumed (created on first use)."""
        return self.consumed.setdefault(hook, set())

    def match_signals(self, sig: Signals, texts: list[str], hook: str) -> list[str] | None:
        """Presence-union score ``hook``'s non-consumed ``texts`` against ``sig``; consume and return
        the contributing texts (window order, deduped by text) on a fire, else ``None``.

        A signal counts once toward ``threshold`` however many entries it matches (union over
        per-entry matches, never a concatenation). Any veto matching any entry — consumed or not —
        suppresses the fire and consumes nothing. Consumption is scoped to ``hook``'s own ledger.
        """
        from captain_hook.signals import matching_signals

        if sig.vetoes and any(matching_signals(sig.vetoes, text) for text in texts):
            return None
        spent = self.consumed.get(hook, frozenset[str]())
        candidates = [
            (text, set(matching_signals(sig.patterns, text))) for text in texts if text_hash(text) not in spent
        ]
        union = {i for _, matched in candidates for i in matched}
        if sum(sig.patterns[i].weight for i in union) < sig.threshold:
            return None
        contributing: list[str] = []
        seen: set[str] = set()
        for text, matched in candidates:
            if matched and (h := text_hash(text)) not in seen:
                seen.add(h)
                contributing.append(text)
        self.consumed_for(hook).update(seen)
        return contributing


def text_hash(text: str) -> str:
    return sha256(text.encode()).hexdigest()[:16]


def package_aware_stem(p: Path) -> str:
    if str(p).startswith(PACKS_DIR):
        return f"{p.parent.name}.{p.stem}"
    if (
        p.name != "__init__.py"
        and not str(p).startswith(FRAMEWORK_DIR)
        and (init := p.parent / "__init__.py").exists()
        and init.stat().st_size > 0
    ):
        return f"{p.parent.name}.{p.stem}"
    return p.stem


def framework_frame(filename: str) -> bool:
    return filename.startswith(FRAMEWORK_DIR) and not filename.startswith(PACKS_DIR)


def caller_stem() -> str:
    frame: FrameType | None = inspect.currentframe()
    if frame:
        frame = frame.f_back
    while frame and framework_frame(frame.f_code.co_filename):
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
    return (ps := evt.ctx.s[PrimitiveState].get()) is not None and ps.last_fired_at > len(evt.ctx.t) - len(evt.ctx.turn)


from captain_hook.session import SessionStore  # noqa: E402


@dataclass
class EchoTracker:
    window: int = ECHO_WINDOW
    threshold: float = ECHO_THRESHOLD
    min_overlap: int = ECHO_MIN_OVERLAP

    def saw(self, text: str, *, evt: BaseHookEvent) -> bool:
        ps = evt.ctx.s[PrimitiveState].get()
        return ps is not None and bool(ps.echo_lemmas) and len(evt.ctx.t) < ps.echo_window_end and ps.is_echo(text)

    def record(self, text: str, triggering: Iterable[str], *, evt: BaseHookEvent) -> None:
        ps = evt.ctx.s[PrimitiveState].get(PrimitiveState())
        ps.echo_lemmas = PrimitiveState.content_lemmas(" ".join(triggering)) | PrimitiveState.content_lemmas(text)
        ps.echo_window_end = len(evt.ctx.t) + self.window
        evt.ctx.s[PrimitiveState].set(ps)


class WorkflowState(BaseModel):
    """Base for a pydantic model that bundles one session workflow across several hooks.

    Decorate the subclass with [`workflow_state`][captain_hook.workflow_state] to register it; the
    subclass then carries three event-driven helpers. ``load`` reads the stored state (defaulting to
    a fresh instance), ``save`` writes it, and ``reset`` deletes it.

    Example:
        >>> @workflow_state("review")
        ... class ReviewState(WorkflowState):
        ...     intent: str | None = None
    """

    __workflow_name__: ClassVar[str | None] = None

    @classmethod
    def load(cls, evt: BaseHookEvent) -> Self:
        return evt.ctx.s.load(cls)

    def save(self, evt: BaseHookEvent) -> None:
        evt.ctx.s[type(self)].set(self)

    @classmethod
    def reset(cls, evt: BaseHookEvent) -> None:
        evt.ctx.s[cls].delete()


T = TypeVar("T", bound=WorkflowState)


def workflow_state(name: str) -> Callable[[type[T]], type[T]]:
    def wrap(cls: type[T]) -> type[T]:
        cls.__workflow_name__ = name
        SessionStore.track(cls)
        return cls

    return wrap


class SeenKeys(BaseModel):
    """Session-scoped record of keys observed by ``SessionStore.once``/``unseen``, namespaced by scope."""

    seen: dict[str, list[str]] = Field(default_factory=dict)


SessionStore.track(HookState)
SessionStore.track(PrimitiveState)
SessionStore.track(SeenKeys)
