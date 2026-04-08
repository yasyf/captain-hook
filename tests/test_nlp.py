from __future__ import annotations

import pytest

from captain_hook.signals.nlp import Clause, NlpSignal, Phrase, dep_related, nlp_scan

# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NLP-001 — Phrase stores lowercased lemmas
# ═══════════════════════════════════════════════════════════════════════════════


class TestPhrase:
    def test_single_word_lowercased(self) -> None:
        p = Phrase("Run")
        assert p.lemmas == ("run",)

    def test_multi_word_lowercased(self) -> None:
        p = Phrase("Run", "TEST")
        assert p.lemmas == ("run", "test")

    def test_frozen(self) -> None:
        p = Phrase("hello")
        with pytest.raises(AttributeError):
            p.lemmas = ("world",)  # type: ignore[misc]

    def test_slotted(self) -> None:
        p = Phrase("hello")
        with pytest.raises((AttributeError, TypeError)):
            p.extra = "nope"  # type: ignore[attr-defined]

    def test_preserves_spaces_in_compound(self) -> None:
        p = Phrase("rate limit")
        assert p.lemmas == ("rate limit",)


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NLP-002 — Phrase.expand uses WordNet synonyms
# ═══════════════════════════════════════════════════════════════════════════════


class TestPhraseExpand:
    def test_expand_includes_original_term(self) -> None:
        p = Phrase.expand("issue")
        assert "issue" in p.lemmas

    def test_expand_includes_synonyms(self) -> None:
        p = Phrase.expand("issue")
        assert len(p.lemmas) > 1
        assert any(syn in p.lemmas for syn in ("consequence", "effect", "outcome"))

    def test_expand_verb_pos(self) -> None:
        p = Phrase.expand("change", pos="v")
        assert "change" in p.lemmas
        assert any(syn in p.lemmas for syn in ("alter", "modify"))

    def test_expand_lowercases(self) -> None:
        p = Phrase.expand("Issue")
        assert all(lemma == lemma.lower() for lemma in p.lemmas)

    def test_expand_multi_word_lemmas_use_spaces(self) -> None:
        p = Phrase.expand("issue")
        for lemma in p.lemmas:
            assert "_" not in lemma

    def test_expand_unknown_word_returns_original(self) -> None:
        p = Phrase.expand("xyzzyplugh")
        assert p.lemmas == ("xyzzyplugh",)


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NLP-003 — Clause validates required components
# ═══════════════════════════════════════════════════════════════════════════════


class TestClauseValidation:
    def test_bare_single_noun_rejected(self) -> None:
        with pytest.raises(ValueError, match="verb, adj, negated, or a compound"):
            Clause(noun=Phrase("quota"))

    def test_verb_clause_valid(self) -> None:
        c = Clause(noun=Phrase("quota"), verb=Phrase("exceed"))
        assert c.verb is not None

    def test_adj_clause_valid(self) -> None:
        c = Clause(noun=Phrase("service"), adj=Phrase("external"))
        assert c.adj is not None

    def test_negated_clause_valid(self) -> None:
        c = Clause(noun=Phrase("problem"), negated=True)
        assert c.negated is True

    def test_compound_noun_valid(self) -> None:
        c = Clause(noun=Phrase("billing issue"))
        assert " " in c.noun.lemmas[0]

    def test_multi_word_noun_with_single_words_rejected(self) -> None:
        with pytest.raises(ValueError, match="verb, adj, negated, or a compound"):
            Clause(noun=Phrase("api", "service"))


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NLP-004 — NlpSignal bundles clauses with weight
# ═══════════════════════════════════════════════════════════════════════════════


class TestNlpSignal:
    def test_default_weight(self) -> None:
        sig = NlpSignal(clauses=[Clause(noun=Phrase("quota"), verb=Phrase("exceed"))])
        assert sig.weight == 1

    def test_custom_weight(self) -> None:
        sig = NlpSignal(clauses=[Clause(noun=Phrase("quota"), verb=Phrase("exceed"))], weight=3)
        assert sig.weight == 3

    def test_frozen(self) -> None:
        sig = NlpSignal(clauses=[Clause(noun=Phrase("quota"), verb=Phrase("exceed"))])
        with pytest.raises(AttributeError):
            sig.weight = 5  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NLP-005 — nlp_scan sentence-level clause matching
# ═══════════════════════════════════════════════════════════════════════════════


class TestNlpScan:
    def test_matches_verb_noun(self) -> None:
        result = nlp_scan(
            [Clause(noun=Phrase("quota"), verb=Phrase("exceed"))],
            "The quota exceeded the threshold",
        )
        assert len(result) == 1
        assert "quota" in result[0].lower()

    def test_no_match_returns_empty(self) -> None:
        result = nlp_scan(
            [Clause(noun=Phrase("quota"), verb=Phrase("exceed"))],
            "Everything is working fine",
        )
        assert result == []

    def test_returns_matching_sentences(self) -> None:
        text = "Everything is fine. The API quota was exceeded. Continuing work."
        result = nlp_scan(
            [Clause(noun=Phrase("quota"), verb=Phrase("exceed"))],
            text,
        )
        assert len(result) == 1
        assert "quota" in result[0].lower()

    def test_multiple_sentences_match(self) -> None:
        result = nlp_scan(
            [
                Clause(noun=Phrase("quota"), verb=Phrase("exceed")),
                Clause(noun=Phrase("api"), adj=Phrase("unavailable")),
            ],
            "The quota exceeded the limit. Also the API is unavailable.",
        )
        assert len(result) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NLP-006 — nlp_scan empty text returns empty list
# ═══════════════════════════════════════════════════════════════════════════════


class TestNlpScanEmpty:
    def test_empty_string(self) -> None:
        assert nlp_scan([Clause(noun=Phrase("quota"), verb=Phrase("exceed"))], "") == []

    def test_whitespace_only(self) -> None:
        assert nlp_scan([Clause(noun=Phrase("quota"), verb=Phrase("exceed"))], "   \n\t  ") == []


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NLP-007 — nlp_scan negation detection
# ═══════════════════════════════════════════════════════════════════════════════


class TestNlpScanNegation:
    def test_matches_negated_sentence(self) -> None:
        result = nlp_scan(
            [Clause(noun=Phrase("problem"), negated=True)],
            "This is not a problem with our code",
        )
        assert len(result) == 1

    def test_no_match_without_negation(self) -> None:
        result = nlp_scan(
            [Clause(noun=Phrase("problem"), negated=True)],
            "There is a problem with our code",
        )
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NLP-008 — nlp_scan compound noun matching
# ═══════════════════════════════════════════════════════════════════════════════


class TestNlpScanCompound:
    def test_compound_noun_match(self) -> None:
        result = nlp_scan(
            [Clause(noun=Phrase("rate limit"), verb=Phrase("hit"))],
            "We hit the rate limit",
        )
        assert len(result) == 1

    def test_compound_noun_only(self) -> None:
        result = nlp_scan(
            [Clause(noun=Phrase("service outage"))],
            "There was a service outage",
        )
        assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NLP-009 — dep_related dependency tree proximity
# ═══════════════════════════════════════════════════════════════════════════════


class TestDepRelated:
    def test_related_verb_object(self) -> None:
        import spacy

        nlp = spacy.load("en_core_web_sm")
        doc = nlp("The quota exceeded the threshold")
        quota = [t for t in doc if t.text == "quota"][0]
        exceeded = [t for t in doc if t.text == "exceeded"][0]
        assert dep_related(quota, exceeded, max_hops=3)

    def test_unrelated_distant_tokens(self) -> None:
        import spacy

        nlp = spacy.load("en_core_web_sm")
        doc = nlp("The big red house on the hill near the old river was beautiful in the evening")
        big = [t for t in doc if t.text == "big"][0]
        evening = [t for t in doc if t.text == "evening"][0]
        assert not dep_related(big, evening, max_hops=1)

    def test_self_related(self) -> None:
        import spacy

        nlp = spacy.load("en_core_web_sm")
        doc = nlp("Hello world")
        hello = doc[0]
        assert dep_related(hello, hello, max_hops=1)


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NLP-010 — Multiple clauses use OR semantics
# ═══════════════════════════════════════════════════════════════════════════════


class TestNlpScanMultipleClauses:
    def test_or_semantics(self) -> None:
        clauses = [
            Clause(noun=Phrase("quota"), verb=Phrase("exceed")),
            Clause(noun=Phrase("service"), adj=Phrase("external")),
        ]
        text = "The quota was exceeded. The weather is nice. An external service caused issues."
        result = nlp_scan(clauses, text)
        assert len(result) == 2

    def test_single_clause_matches_single_sentence(self) -> None:
        clauses = [
            Clause(noun=Phrase("quota"), verb=Phrase("exceed")),
            Clause(noun=Phrase("api"), adj=Phrase("unavailable")),
        ]
        text = "The quota was exceeded. Everything else works fine."
        result = nlp_scan(clauses, text)
        assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Additional behavioral tests from expectedBehavior
# ═══════════════════════════════════════════════════════════════════════════════


class TestActivePassiveVoice:
    def test_active_voice(self) -> None:
        assert nlp_scan(
            [Clause(noun=Phrase("quota"), verb=Phrase("exceed"))],
            "The quota exceeded the threshold",
        )

    def test_passive_voice(self) -> None:
        assert nlp_scan(
            [Clause(noun=Phrase("quota"), verb=Phrase("exceed"))],
            "The quota was exceeded",
        )


class TestCopularAdjectives:
    def test_copular_adj(self) -> None:
        assert nlp_scan(
            [Clause(noun=Phrase("api"), adj=Phrase("unavailable"))],
            "The API is unavailable",
        )

    def test_attributive_adj(self) -> None:
        assert nlp_scan(
            [Clause(noun=Phrase("service"), adj=Phrase("external"))],
            "An external service caused the error",
        )


class TestIntegrationWithScoreSignals:
    def test_nlp_signal_in_score_signals(self) -> None:
        from captain_hook.signals import score_signals

        patterns = [
            NlpSignal(
                clauses=[Clause(noun=Phrase("quota"), verb=Phrase("exceed"))],
                weight=3,
            ),
        ]
        assert score_signals(patterns, "The quota was exceeded") == 3
        assert score_signals(patterns, "Everything is fine") == 0

    def test_nlp_signal_in_extract_signal_context(self) -> None:
        from captain_hook.signals import extract_signal_context

        patterns = [
            NlpSignal(
                clauses=[Clause(noun=Phrase("quota"), verb=Phrase("exceed"))],
                weight=3,
            ),
        ]
        result = extract_signal_context(patterns, "Normal text. The quota was exceeded. More text.")
        assert len(result) == 1
        assert "quota" in result[0].lower()
