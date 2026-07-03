from __future__ import annotations

import functools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from spacy.tokens import Doc, Span, Token


@dataclass(frozen=True, slots=True)
class PhraseFields:
    lemmas: tuple[str, ...]


class Phrase(PhraseFields):
    """A set of lowercased lemmas naming one concept, matched against tokens by lemma.

    Multi-word terms ("rate limit") match a head token whose ``compound``
    children supply the remaining words.

    Example:
        >>> Phrase("remove", "delete", "drop")
    """

    __slots__ = ()

    def __init__(self, *terms: str) -> None:
        super().__init__(tuple(t.lower() for t in terms))

    @classmethod
    def expand(cls, *terms: str, pos: str = "n") -> Phrase:
        """Phrase covering ``terms`` plus their WordNet synonyms for ``pos``.

        Example:
            >>> Phrase.expand("issue")  # issue, consequence, effect, outcome, ...
        """
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


@dataclass(frozen=True, slots=True, kw_only=True)
class Clause:
    """One dependency-aware pattern matched against each sentence of a text.

    Anchors on ``noun`` when set (a NOUN/PROPN lemma hit), else on ``verb``
    (a VERB/AUX lemma hit); every other constraint must hold on a token
    dependency-related to the anchor.

    Attributes:
        noun: Noun phrase the clause anchors on, when set.
        verb: Verb phrase; the anchor when ``noun`` is unset, otherwise a
            constraint dependency-related to the noun anchor.
        adj: Adjective/adverb phrase (pos ADJ/ADV/PART) related to the anchor.
        negated: Require a negation (``neg`` dependency) related to the anchor.
        completed: Only match verbs reported as done (see ``is_past_predicate``) —
            "removed the retry logic" but not "remove the node" or
            "will be removed later".
        subject: ``"no_nominal"`` vetoes verbs with a substantive active subject
            (see ``has_nominal_subject``) — "the parser removed the node" is
            vetoed while "we removed it" and passives still match.

    Example:
        >>> Clause(verb=Phrase("remove", "delete"), completed=True, subject="no_nominal")
    """

    noun: Phrase | None = None
    verb: Phrase | None = None
    adj: Phrase | None = None
    negated: bool = False
    completed: bool = False
    subject: Literal["any", "no_nominal"] = "any"

    def __post_init__(self) -> None:
        if (anchor := self.noun or self.verb) is None:
            raise ValueError("Clause needs a noun or verb anchor")
        if self.verb is None and (self.completed or self.subject != "any"):
            raise ValueError("Clause completed and subject constraints require a verb")
        if not (
            (self.noun and self.verb)
            or self.adj
            or self.negated
            or self.completed
            or self.subject != "any"
            or any(" " in lemma for lemma in anchor.lemmas)
        ):
            raise ValueError("Clause needs a second constraint or a compound anchor phrase")


@dataclass(frozen=True, kw_only=True)
class NlpSignal:
    """A transcript signal that scores ``weight`` when any clause matches a sentence.

    Example:
        >>> NlpSignal(clauses=[Clause(noun=Phrase("quota"), verb=Phrase("exceed"))], weight=2)
    """

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


def is_past_predicate(tok: Token) -> bool:
    """Whether ``tok`` reports a completed action.

    True for a past-tense or participial predicate (tag ``VBD``/``VBN``) used
    predicatively (``dep_`` is not ``amod``) with no present-tense or modal
    auxiliary child — so "removed the retry logic" and "was moved to utils.py"
    qualify while "removes stale entries", "is removed when it expires", and
    "will be moved later" do not.
    """
    return (
        tok.tag_ in {"VBD", "VBN"}
        and tok.dep_ != "amod"
        and not any(
            c.dep_ in {"aux", "auxpass"} and (c.morph.get("Tense") == ["Pres"] or c.tag_ == "MD") for c in tok.children
        )
    )


def has_nominal_subject(tok: Token) -> bool:
    """Whether ``tok`` has a substantive active subject.

    True when ``tok`` has a letter-bearing, non-pronoun ``nsubj`` child —
    "the parser removed the node" has one, while pronoun subjects
    ("we removed it") and passive subjects (``nsubjpass``) do not count.
    """
    return any(c.dep_ == "nsubj" and c.pos_ != "PRON" and any(ch.isalpha() for ch in c.text) for c in tok.children)


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


def verb_candidates(clause: Clause, sent: Span) -> list[Token]:
    if clause.verb is None:
        return []
    return [
        v
        for v in find_lemma_matches(clause.verb, sent, {"VERB", "AUX"})
        if (not clause.completed or is_past_predicate(v)) and (clause.subject == "any" or not has_nominal_subject(v))
    ]


def match_clause(clause: Clause, sent: Span) -> bool:
    verbs = verb_candidates(clause, sent)
    anchors = find_lemma_matches(clause.noun, sent, {"NOUN", "PROPN"}) if clause.noun else verbs
    return any(
        (not (clause.noun and clause.verb) or any(dep_related(anchor, v) for v in verbs))
        and (
            not clause.adj
            or any(dep_related(anchor, a) for a in find_lemma_matches(clause.adj, sent, {"ADJ", "ADV", "PART"}))
        )
        and (not clause.negated or any(t.dep_ == "neg" and dep_related(anchor, t) for t in sent))
        for anchor in anchors
    )


def nlp_scan(clauses: Sequence[Clause], text: str) -> list[str]:
    if not text.strip():
        return []
    return [sent.text.strip() for sent in parse(text).sents if any(match_clause(clause, sent) for clause in clauses)]
