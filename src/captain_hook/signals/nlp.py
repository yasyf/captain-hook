from __future__ import annotations

import functools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spacy.tokens import Doc, Span, Token

__all__ = ["Clause", "NlpSignal", "Phrase", "dep_related", "nlp_scan"]

WN_LOADED = False


def ensure_wn() -> None:
    import wn

    global WN_LOADED  # noqa: PLW0603
    if not WN_LOADED:
        wn.download("oewn:2025", progress_handler=None)
        WN_LOADED = True  # pyright: ignore[reportConstantRedefinition]


@dataclass(frozen=True, slots=True, init=False)
class Phrase:
    """A set of lowercased lemmas for NLP matching. Use ``Phrase.expand()`` to add WordNet synonyms."""

    lemmas: tuple[str, ...]

    def __init__(self, *terms: str) -> None:
        object.__setattr__(self, "lemmas", tuple(t.lower() for t in terms))

    @classmethod
    def expand(cls, *terms: str, pos: str = "n") -> Phrase:
        import wn

        ensure_wn()
        return cls(
            *{lemma.replace("_", " ") for term in terms for ss in wn.synsets(term, pos=pos) for lemma in ss.lemmas()}
            | {t.lower() for t in terms}
        )


@dataclass(frozen=True, slots=True)
class Clause:
    """An NLP clause matching a noun with optional verb, adjective, or negation via spaCy dependency parsing."""

    noun: Phrase
    verb: Phrase | None = None
    adj: Phrase | None = None
    negated: bool = False

    def __post_init__(self) -> None:
        if not self.verb and not self.adj and not self.negated and not any(" " in lemma for lemma in self.noun.lemmas):
            raise ValueError("Clause needs verb, adj, negated, or a compound noun phrase")


@dataclass(frozen=True, kw_only=True)
class NlpSignal:
    """An NLP-based signal pattern: a set of clauses matched via spaCy, contributing ``weight`` to the score."""

    clauses: Sequence[Clause]
    weight: int = 1


@functools.lru_cache
def parse(text: str) -> Doc:
    from captain_hook.state import get_nlp

    return get_nlp()(text)


def ancestors(tok: Token, max_hops: int) -> set[Token]:
    result = {tok}
    node = tok
    for _ in range(max_hops):
        if node == node.head:
            break
        result.add(node := node.head)
    return result


def dep_related(a: Token, b: Token, max_hops: int = 3) -> bool:
    """Check whether two spaCy tokens are related within ``max_hops`` in the dependency tree.

    Args:
        a: First token.
        b: Second token.
        max_hops: Maximum ancestor hops to traverse (default 3).

    Returns:
        True if the tokens share a common ancestor within the hop limit.
    """
    return bool(ancestors(a, max_hops) & ancestors(b, max_hops))


def find_lemma_matches(phrase: Phrase, sent: Span, pos: set[str]) -> list[Token]:
    return [
        tok
        for lemma in phrase.lemmas
        if (parts := lemma.split())
        for tok in sent
        if tok.pos_ in pos
        and tok.lemma_.lower() == parts[-1]
        and (
            len(parts) == 1
            or all(m in {c.lemma_.lower() for c in tok.children if c.dep_ == "compound"} for m in parts[:-1])
        )
    ]


def match_clause(clause: Clause, sent: Span) -> bool:
    return any(
        (not clause.verb or any(dep_related(nt, v) for v in find_lemma_matches(clause.verb, sent, {"VERB"})))
        and (
            not clause.adj
            or any(dep_related(nt, a) for a in find_lemma_matches(clause.adj, sent, {"ADJ", "ADV", "PART"}))
        )
        and (not clause.negated or any(t.dep_ == "neg" and dep_related(nt, t) for t in sent))
        for nt in find_lemma_matches(clause.noun, sent, {"NOUN", "PROPN"})
    )


def nlp_scan(clauses: Sequence[Clause], text: str) -> list[str]:
    """Scan text for sentences matching any of the given NLP clauses.

    Uses spaCy dependency parsing to find sentences where noun-verb-adj
    relationships match clause specifications. Multiple clauses use OR semantics.

    Args:
        clauses: Clause instances defining noun/verb/adj/negation patterns.
        text: Text to scan (split into sentences by spaCy).

    Returns:
        List of matching sentence strings (empty for blank input).
    """
    if not text.strip():
        return []
    return [sent.text.strip() for sent in parse(text).sents if any(match_clause(clause, sent) for clause in clauses)]
