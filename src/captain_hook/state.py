from __future__ import annotations

import inspect
import re
from functools import cached_property
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from types import FrameType, ModuleType

    import spacy

    from captain_hook.events import BaseHookEvent
    from captain_hook.types import Signals

FRAMEWORK_DIR = str(Path(__file__).resolve().parent)
SPACY_CACHE = Path.home() / ".cache" / "spacy"


class NlpResources:
    @cached_property
    def spacy(self) -> spacy.language.Language:
        import sys

        import spacy

        SPACY_CACHE.mkdir(parents=True, exist_ok=True)
        if (cache := str(SPACY_CACHE)) not in sys.path:
            sys.path.insert(0, cache)
        try:
            return spacy.load("en_core_web_sm")
        except OSError:
            self.install_spacy_model("en_core_web_sm")
            return spacy.load("en_core_web_sm")

    @cached_property
    def wn(self) -> ModuleType:
        import wn

        if not wn.lexicons(lexicon="oewn:2025"):
            wn.download("oewn:2025", progress_handler=None)
        return wn

    @staticmethod
    def install_spacy_model(name: str) -> None:
        from spacy.cli.download import download

        download(name, False, False, None, "--target", str(SPACY_CACHE))


RESOURCES = NlpResources()


class HookState(BaseModel):
    fire_count: int = 0


ECHO_WINDOW = 5
ECHO_THRESHOLD = 0.4
ECHO_MIN_OVERLAP = 2


class PrimitiveState(BaseModel):
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
