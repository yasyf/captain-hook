from __future__ import annotations

import functools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spacy.tokens import Doc, Span, Token


@dataclass(frozen=True, slots=True)
class PhraseFields:
    lemmas: tuple[str, ...]


class Phrase(PhraseFields):
    __slots__ = ()

    def __init__(self, *terms: str) -> None:
        super().__init__(tuple(t.lower() for t in terms))

    @classmethod
    def expand(cls, *terms: str, pos: str = "n") -> Phrase:
        from captain_hook.state import RESOURCES

        return cls(
            *{
                lemma.replace("_", " ")
                for term in terms
                for ss in RESOURCES.wn.synsets(term, pos=pos)
                for lemma in ss.lemmas()
            }
            | {t.lower() for t in terms}
        )


@dataclass(frozen=True, slots=True)
class Clause:
    noun: Phrase
    verb: Phrase | None = None
    adj: Phrase | None = None
    negated: bool = False

    def __post_init__(self) -> None:
        if not self.verb and not self.adj and not self.negated and not any(" " in lemma for lemma in self.noun.lemmas):
            raise ValueError("Clause needs verb, adj, negated, or a compound noun phrase")


@dataclass(frozen=True, kw_only=True)
class NlpSignal:
    clauses: Sequence[Clause]
    weight: int = 1


@functools.lru_cache
def parse(text: str) -> Doc:
    from captain_hook.state import RESOURCES

    return RESOURCES.spacy(text)


def ancestors(tok: Token, max_hops: int) -> set[Token]:
    result = {tok}
    node = tok
    for _ in range(max_hops):
        if node == node.head:
            break
        result.add(node := node.head)
    return result


def dep_related(a: Token, b: Token, max_hops: int = 3) -> bool:
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
    if not text.strip():
        return []
    return [sent.text.strip() for sent in parse(text).sents if any(match_clause(clause, sent) for clause in clauses)]
