"""Hook fire-count and primitive echo-suppression state, plus shared NLP resources (spaCy, WordNet)."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
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
# TURN_ECHO_LOOKBACK: assumed event span of a window="turn" pass, added to ECHO_WINDOW to size
# forward echo damping when the turn's real length is unknown at fire time (a sum, not a max).
# ECHO_VERBATIM_CAP: fired-warn sentences kept for verbatim-quote damping, FIFO session-wide; oldest evict.
# ECHO_VERBATIM_MIN_CHARS: shortest warn sentence seeded, so a generic fragment can't damp by
# containment; also the floor below which a strip-and-score remainder counts as a pure echo.
ECHO_WINDOW = 5
ECHO_THRESHOLD = 0.4
ECHO_MIN_OVERLAP = 2
TURN_ECHO_LOOKBACK = 40
ECHO_VERBATIM_CAP = 40
ECHO_VERBATIM_MIN_CHARS = 20


class PrimitiveState(BaseModel):
    """Per-primitive nudge/gate state shared across all hooks in a session.

    ``last_fired_at`` (the session-global turn-throttle read by every LLM hook and
    blocking gate) and the ``echo_lemmas``/``echo_window_end`` window (a global
    last-write-wins "don't re-trigger on a parrot of the last nudge" filter) are
    deliberately session-scoped, not per hook. ``echo_verbatim`` is the session-wide,
    cross-hook FIFO of fired-warn sentences that damps a later verbatim quote of any
    hook's warning, independent of the lemma window. ``consumed`` is a per-hook-name
    ledger of signal-text hashes each hook has already scored, so one hook's aggregate
    fire cannot mute another's signals — access it through :meth:`consumed_for`.
    """

    last_fired_at: int = 0
    consumed: dict[str, set[str]] = Field(default_factory=dict)
    echo_lemmas: set[str] = Field(default_factory=set)
    echo_window_end: int = 0
    echo_verbatim: list[str] = Field(default_factory=list)

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

    def strip_fired_output(self, text: str) -> str:
        """The candidate content after removing any seeded fired-warn sentences.

        Returns *text* unchanged when the ledger is empty or no seeded sentence is contained
        (Allow-side precision — shared words alone never touch it). When a seeded sentence is
        contained it is deleted from the whitespace-normalized text and the remainder returned;
        an empty or sub-floor remainder means a pure quote of fired output, returned as ``""`` so
        callers drop it. A genuinely new violation riding alongside a quoted warn survives as the
        stripped remainder and is still scored.
        """
        if not self.echo_verbatim or not (normalized := normalize_ws(text)):
            return text
        stripped = normalized
        for sentence in self.echo_verbatim:
            stripped = stripped.replace(sentence, " ")
        if (stripped := normalize_ws(stripped)) == normalized:
            return text
        return stripped if len(stripped) >= ECHO_VERBATIM_MIN_CHARS else ""

    def unechoed_candidates(self, texts: list[str]) -> list[str]:
        """Map *texts* through fired-output stripping, dropping pure quotes — the containment-only
        candidate filter shared by the llm pre-gate and post-verdict consumption so the two agree on
        which texts are eligible (a divergence would let a quote veto or absorb a real violation)."""
        return [stripped for text in texts if (stripped := self.strip_fired_output(text))]

    def seed_echo_window(self, triggering_texts: list[str], message: str, transcript_len: int) -> None:
        self.echo_lemmas = self.content_lemmas(" ".join(triggering_texts)) | self.content_lemmas(message)
        self.echo_window_end = transcript_len + ECHO_WINDOW

    def seed_echo_verbatim(self, message: str) -> None:
        seen = set(self.echo_verbatim)
        fresh = [
            norm
            for sent in RESOURCES.spacy(message).sents
            if len(norm := normalize_ws(sent.text)) >= ECHO_VERBATIM_MIN_CHARS
            and norm not in seen
            and not seen.add(norm)
        ]
        self.echo_verbatim = (self.echo_verbatim + fresh)[-ECHO_VERBATIM_CAP:]

    def consumed_for(self, hook: str) -> set[str]:
        """The mutable set of signal-text hashes ``hook`` has already consumed (created on first use)."""
        return self.consumed.setdefault(hook, set())

    def match_signals(self, sig: Signals, texts: list[str], hook: str) -> list[str] | None:
        """Score ``hook``'s non-consumed ``texts`` against ``sig``; consume and return the
        contributing texts (window order, deduped by text) on a fire, else ``None``.

        Under the default ``scope="text"`` an entry qualifies when the weights of its own
        matched signals reach ``threshold``, and only qualifying entries are consumed —
        sub-threshold matches stay live for a later pass. Under ``scope="window"`` a signal
        counts once toward ``threshold`` however many entries it matches (presence-union over
        the window), and every matching entry is consumed on a fire. Any veto matching any
        entry — consumed or not — suppresses the fire and consumes nothing under either scope.
        Consumption is scoped to ``hook``'s own ledger.
        """
        from captain_hook.signals import matching_signals

        if sig.vetoes and any(matching_signals(sig.vetoes, text) for text in texts):
            return None
        spent = self.consumed.get(hook, frozenset[str]())
        candidates = [
            (text, set(matching_signals(sig.patterns, text))) for text in texts if text_hash(text) not in spent
        ]
        match sig.scope:
            case "window":
                union = {i for _, matched in candidates for i in matched}
                if sum(sig.patterns[i].weight for i in union) < sig.threshold:
                    return None
                qualifying = [(text, matched) for text, matched in candidates if matched]
            case "text":
                qualifying = [
                    (text, matched)
                    for text, matched in candidates
                    if sum(sig.patterns[i].weight for i in matched) >= sig.threshold
                ]
                if not qualifying:
                    return None
        contributing: list[str] = []
        seen: set[str] = set()
        for text, _ in qualifying:
            if (h := text_hash(text)) not in seen:
                seen.add(h)
                contributing.append(text)
        self.consumed_for(hook).update(seen)
        return contributing


def normalize_ws(text: str) -> str:
    return " ".join(text.split())


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
    resolved = Path(filename).resolve()
    return resolved.is_relative_to(FRAMEWORK_DIR) and not resolved.is_relative_to(PACKS_DIR)


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
    with evt.ctx.s[PrimitiveState].mutate() as ps:
        ps.last_fired_at = len(evt.ctx.t)


def fired_this_turn(evt: BaseHookEvent) -> bool:
    return (ps := evt.ctx.s[PrimitiveState].get()) is not None and ps.last_fired_at > len(evt.ctx.t) - len(evt.ctx.turn)


from captain_hook.session import SessionStore  # noqa: E402


@dataclass
class EchoTracker:
    window: int = ECHO_WINDOW
    threshold: float = ECHO_THRESHOLD
    min_overlap: int = ECHO_MIN_OVERLAP

    def surviving(self, text: str, *, evt: BaseHookEvent) -> str | None:
        """The echo-stripped remainder of *text* to score, or ``None`` when it is a pure verbatim
        quote of fired output or a lemma echo of the last nudge within the forward window. A quoted
        warn carrying a genuinely new violation survives as the stripped remainder."""
        ps = evt.ctx.s[PrimitiveState].get()
        if ps is None:
            return text
        if not (remainder := ps.strip_fired_output(text)):
            return None
        if ps.echo_lemmas and len(evt.ctx.t) < ps.echo_window_end and ps.is_echo(remainder):
            return None
        return remainder

    def record(self, text: str, triggering: Iterable[str], *, evt: BaseHookEvent) -> None:
        with evt.ctx.s[PrimitiveState].mutate() as ps:
            ps.echo_lemmas = PrimitiveState.content_lemmas(" ".join(triggering)) | PrimitiveState.content_lemmas(text)
            ps.echo_window_end = len(evt.ctx.t) + self.window
            ps.seed_echo_verbatim(text)


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

    @classmethod
    @contextmanager
    def mutate(cls, evt: BaseHookEvent) -> Iterator[Self]:
        """Yield the stored workflow state under an exclusive lock; persist it on clean exit.

        Reach for this over ``load``/``save`` when several hooks race the same workflow record
        within a session and a lost update would corrupt it. Non-reentrant: nesting ``mutate()`` on
        the same slot within one process deadlocks — the file lock is held for the whole block and a
        second acquire of the same path (default ``timeout=-1``) blocks forever.
        """
        with evt.ctx.s[cls].mutate() as obj:
            yield obj


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
