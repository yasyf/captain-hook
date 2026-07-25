from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import pytest

from captain_hook.signals.nlp import (
    Clause,
    NlpSignal,
    Phrase,
    dep_related,
    has_nominal_subject,
    is_past_predicate,
    nlp_scan,
    parse,
    scan_text,
    subject_kind,
)

if TYPE_CHECKING:
    from spacy.tokens import Token

TOMBSTONE_CLAUSE = Clause(
    verb=Phrase("remove", "delete", "drop", "move", "migrate", "rename"),
    tense="completed",
    subject=("unnamed", "passive"),
)

BE_ADVERB_CLAUSE = Clause(
    verb=Phrase("be"),
    adj=Phrase("previously", "formerly", "originally", "here"),
    tense="completed",
)

LEAVE_PROSPECTIVE_CLAUSE = Clause(verb=Phrase("leave"), tense="prospective")

SWITCH_MODE_CLAUSE = Clause(
    noun=Phrase("mode"),
    verb=Phrase("enter", "switch", "return", "go"),
    subject=("unnamed",),
)

COMMENT_LEADERS = [
    pytest.param("", id="bare"),
    pytest.param("# ", id="hash"),
    pytest.param("// ", id="double_slash"),
    pytest.param("/// ", id="triple_slash"),
    pytest.param("/* ", id="slash_star"),
]


def token_named(text: str, word: str) -> Token:
    return next(t for t in parse(text) if t.text == word)


class TestPhrase:
    @pytest.mark.parametrize(
        ("words", "lemmas"),
        [
            pytest.param(("Run",), ("run",), id="single_word_lowercased"),
            pytest.param(("Run", "TEST"), ("run", "test"), id="multi_word_lowercased"),
            pytest.param(("rate limit",), ("rate limit",), id="preserves_spaces_in_compound"),
        ],
    )
    def test_lemmas(self, words: tuple[str, ...], lemmas: tuple[str, ...]) -> None:
        assert Phrase(*words).lemmas == lemmas

    def test_frozen(self) -> None:
        p = Phrase("hello")
        with pytest.raises(AttributeError):
            p.lemmas = ("world",)  # type: ignore[misc]

    def test_slotted(self) -> None:
        p = Phrase("hello")
        with pytest.raises((AttributeError, TypeError)):
            p.extra = "nope"  # type: ignore[attr-defined]


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

    def test_expand_usable_on_worker_thread(self) -> None:
        assert any(syn in Phrase.expand("change", pos="v").lemmas for syn in ("alter", "modify"))

        with ThreadPoolExecutor(max_workers=1) as pool:
            lemmas = pool.submit(lambda: Phrase.expand("modify", pos="v").lemmas).result(timeout=30)
        assert any(syn in lemmas for syn in ("change", "alter"))


class TestClauseValidation:
    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            pytest.param({"noun": Phrase("quota")}, "second constraint or a compound", id="bare_single_noun_rejected"),
            pytest.param(
                {"noun": Phrase("api", "service")},
                "second constraint or a compound",
                id="multi_word_noun_with_single_words_rejected",
            ),
            pytest.param({"verb": Phrase("remove")}, "second constraint or a compound", id="bare_single_verb_rejected"),
            pytest.param({}, "noun or verb anchor", id="no_anchor_rejected"),
            pytest.param(
                {"noun": Phrase("quota"), "tense": "completed"}, "require a verb", id="completed_without_verb_rejected"
            ),
            pytest.param(
                {"noun": Phrase("quota"), "tense": "prospective"},
                "require a verb",
                id="prospective_without_verb_rejected",
            ),
            pytest.param(
                {"noun": Phrase("quota"), "subject": "unnamed"}, "require a verb", id="subject_without_verb_rejected"
            ),
            pytest.param(
                {"verb": Phrase("remove"), "subject": ("named",)},
                "Unknown subject kinds",
                id="unknown_subject_kind_rejected",
            ),
        ],
    )
    def test_rejected(self, kwargs: dict[str, Any], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            Clause(**kwargs)

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
        assert c.noun is not None
        assert " " in c.noun.lemmas[0]

    def test_verb_completed_valid(self) -> None:
        c = Clause(verb=Phrase("remove"), tense="completed")
        assert c.tense == "completed"

    def test_verb_prospective_valid(self) -> None:
        c = Clause(verb=Phrase("leave"), tense="prospective")
        assert c.tense == "prospective"

    def test_tense_defaults_to_any(self) -> None:
        c = Clause(noun=Phrase("quota"), verb=Phrase("exceed"))
        assert c.tense == "any"

    def test_verb_subject_valid(self) -> None:
        c = Clause(verb=Phrase("remove"), subject=("unnamed", "passive"))
        assert c.subject == ("unnamed", "passive")

    def test_verb_subject_string_normalized(self) -> None:
        c = Clause(verb=Phrase("remove"), subject="unnamed")
        assert c.subject == ("unnamed",)

    def test_compound_verb_valid(self) -> None:
        c = Clause(verb=Phrase("garbage collect"))
        assert c.verb is not None
        assert " " in c.verb.lemmas[0]


class TestPastPredicate:
    @pytest.mark.parametrize(
        ("text", "word", "expected"),
        [
            pytest.param("removed the retry logic", "removed", True, id="past_tense_root"),
            pytest.param("was moved to utils.py", "moved", True, id="passive_participle"),
            pytest.param("remove the node", "remove", False, id="imperative_infinitive"),
            pytest.param("removes stale entries", "removes", False, id="present_habitual"),
            pytest.param("is removed when it expires", "removed", False, id="present_passive_aux"),
            pytest.param("will be moved to utils.py later", "moved", False, id="modal_future_will"),
            pytest.param("should be removed eventually", "removed", False, id="modal_should"),
            pytest.param("can be removed once v2 ships", "removed", False, id="modal_can"),
            pytest.param("the removed entries were stale", "removed", False, id="attributive_amod"),
            pytest.param("retry logic has been moved to utils.py", "moved", True, id="present_perfect_passive_moved"),
            pytest.param("this function has been removed", "removed", True, id="present_perfect_passive_removed"),
            pytest.param("the fallback path has been deleted", "deleted", True, id="present_perfect_passive_deleted"),
        ],
    )
    def test_predicate(self, text: str, word: str, expected: bool) -> None:
        assert is_past_predicate(token_named(text, word)) is expected


class TestSubjectGate:
    @pytest.mark.parametrize(
        ("text", "word", "expected"),
        [
            pytest.param("the parser removed the node", "removed", True, id="nominal_subject"),
            pytest.param("skips removed entries", "removed", True, id="misparsed_propn_subject"),
            pytest.param("handles removed entries", "removed", True, id="misparsed_noun_subject"),
            pytest.param("we removed it", "removed", False, id="pronoun_subject"),
            pytest.param("was moved to utils.py", "moved", False, id="passive_nsubjpass"),
            pytest.param("# removed the retry logic", "removed", False, id="delimiter_nsubj_not_letter_bearing"),
            pytest.param("# config moved to settings.py", "moved", True, id="elliptical_passive_nsubj"),
        ],
    )
    def test_subject(self, text: str, word: str, expected: bool) -> None:
        assert has_nominal_subject(token_named(text, word)) is expected


class TestSubjectKind:
    @pytest.mark.parametrize(
        ("text", "word", "expected"),
        [
            pytest.param("switch back to plan mode", "switch", "unnamed", id="imperative"),
            pytest.param("we removed it", "removed", "unnamed", id="pronoun_subject"),
            pytest.param("the file was removed", "removed", "unnamed", id="true_passive"),
            pytest.param("config moved to settings.py", "moved", "passive", id="elliptical_passive"),
            pytest.param("the daemon switches to degraded mode", "switches", "passive", id="intransitive_active"),
            pytest.param("the parser removed the node", "removed", "actor", id="named_actor"),
        ],
    )
    def test_kind(self, text: str, word: str, expected: str) -> None:
        assert subject_kind(token_named(text, word)) == expected


class TestScanText:
    def test_regex_case_insensitive(self) -> None:
        assert scan_text("Re-enter PLAN MODE now", ["plan mode"])

    def test_regex_no_match(self) -> None:
        assert not scan_text("fix the typo in main.py", [r"plan mode"])

    def test_clause_match(self) -> None:
        assert scan_text("we should return to plan mode", [SWITCH_MODE_CLAUSE])

    def test_clause_no_match(self) -> None:
        assert not scan_text("the app enters sleep mode when idle", [SWITCH_MODE_CLAUSE])

    def test_mixed_patterns(self) -> None:
        assert scan_text("STOP all edits", [SWITCH_MODE_CLAUSE, r"\bstop\b"])

    def test_empty_text(self) -> None:
        assert not scan_text("", [r"stop", SWITCH_MODE_CLAUSE])


class TestSubjectUnnamedScan:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("switch back to plan mode", True, id="imperative"),
            pytest.param("we should return to plan mode", True, id="pronoun_subject"),
            pytest.param("the daemon switches to degraded mode on timeout", False, id="nominal_subject_intransitive"),
            pytest.param("the app enters sleep mode when idle", False, id="nominal_subject_transitive"),
        ],
    )
    def test_scan(self, text: str, expected: bool) -> None:
        assert bool(nlp_scan([SWITCH_MODE_CLAUSE], text)) is expected


class TestVerbAnchoredScan:
    @pytest.mark.parametrize("leader", COMMENT_LEADERS)
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("removed the retry logic", True, id="past_tense_active"),
            pytest.param("was moved to utils.py", True, id="passive_was_moved"),
            pytest.param("deleted the fallback path", True, id="past_tense_deleted"),
            pytest.param("we removed it", True, id="pronoun_subject_passes"),
            pytest.param("moved to utils.py", True, id="bare_participle"),
            pytest.param("remove the node", False, id="imperative"),
            pytest.param("removes stale entries", False, id="present_habitual"),
            pytest.param("is removed when it expires", False, id="present_passive"),
            pytest.param("will be moved to utils.py later", False, id="modal_future_will"),
            pytest.param("should be removed eventually", False, id="modal_should"),
            pytest.param("can be removed once v2 ships", False, id="modal_can"),
            pytest.param("skips removed entries", False, id="nominal_subject_skips"),
            pytest.param("handles removed entries", False, id="nominal_subject_handles"),
            pytest.param("the parser removed the node", False, id="nominal_subject_parser"),
            pytest.param("retry logic has been moved to utils.py", True, id="present_perfect_moved"),
            pytest.param("this function has been removed", True, id="present_perfect_removed"),
            pytest.param("the fallback path has been deleted", True, id="present_perfect_deleted"),
            pytest.param("config moved to settings.py", True, id="elliptical_config_moved"),
            pytest.param("helpers moved to utils.py", True, id="elliptical_helpers_moved"),
            pytest.param("retry handling migrated to backoff.py", True, id="elliptical_handling_migrated"),
            pytest.param("old handler removed", True, id="elliptical_handler_removed"),
            pytest.param("retry logic removed", True, id="elliptical_logic_removed"),
            pytest.param("logic migrated to the worker", True, id="elliptical_logic_migrated"),
        ],
    )
    def test_scan(self, leader: str, text: str, expected: bool) -> None:
        assert bool(nlp_scan([TOMBSTONE_CLAUSE], leader + text)) is expected


class TestProspectiveScan:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("I'll leave the broken test as is", True, id="modal_will_base"),
            pytest.param("leaving the flaky test documented", True, id="gerund"),
            pytest.param("leave the failing test alone", True, id="imperative_base"),
            pytest.param("left the workspace in a broken state", False, id="past_tense_vbd"),
            pytest.param("the test was left to clean up later", False, id="passive_participle_vbn"),
            pytest.param("has left the retry logic in place", False, id="present_perfect_vbn"),
            pytest.param("I should have left the broken test alone", False, id="modal_perfect_counterfactual"),
            pytest.param("I will have left the retry logic by then", True, id="future_perfect_prospective"),
            pytest.param("I won't leave the broken test alone", False, id="negated_prospective"),
        ],
    )
    def test_scan(self, text: str, expected: bool) -> None:
        assert bool(nlp_scan([LEAVE_PROSPECTIVE_CLAUSE], text)) is expected


class TestBeAdverbClause:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("the auth check was previously here", True, id="was_previously_here"),
            pytest.param("this was originally here", True, id="was_originally_here"),
            pytest.param("validation was formerly here", True, id="was_formerly_here"),
            pytest.param("was previously handled here", True, id="bare_passive_was"),
            pytest.param("the auth check is here", False, id="present_copula"),
            pytest.param("the config will be here later", False, id="modal_future"),
            pytest.param("handle the case here", False, id="imperative_no_be"),
        ],
    )
    def test_scan(self, text: str, expected: bool) -> None:
        assert bool(nlp_scan([BE_ADVERB_CLAUSE], text)) is expected


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


class TestNlpScanEmpty:
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("", id="empty_string"),
            pytest.param("   \n\t  ", id="whitespace_only"),
        ],
    )
    def test_no_match(self, text: str) -> None:
        assert nlp_scan([Clause(noun=Phrase("quota"), verb=Phrase("exceed"))], text) == []


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


class TestNlpScanCompound:
    @pytest.mark.parametrize(
        ("clauses", "text"),
        [
            pytest.param(
                [Clause(noun=Phrase("rate limit"), verb=Phrase("hit"))],
                "We hit the rate limit",
                id="compound_noun_match",
            ),
            pytest.param(
                [Clause(noun=Phrase("service outage"))],
                "There was a service outage",
                id="compound_noun_only",
            ),
        ],
    )
    def test_single_match(self, clauses: list[Clause], text: str) -> None:
        assert len(nlp_scan(clauses, text)) == 1


class TestDepRelated:
    def test_related_verb_object(self) -> None:
        from captain_hook.state import RESOURCES

        doc = RESOURCES.spacy("The quota exceeded the threshold")
        quota = [t for t in doc if t.text == "quota"][0]
        exceeded = [t for t in doc if t.text == "exceeded"][0]
        assert dep_related(quota, exceeded, max_hops=3)

    def test_unrelated_distant_tokens(self) -> None:
        from captain_hook.state import RESOURCES

        doc = RESOURCES.spacy("The big red house on the hill near the old river was beautiful in the evening")
        big = [t for t in doc if t.text == "big"][0]
        evening = [t for t in doc if t.text == "evening"][0]
        assert not dep_related(big, evening, max_hops=1)

    def test_self_related(self) -> None:
        from captain_hook.state import RESOURCES

        doc = RESOURCES.spacy("Hello world")
        hello = doc[0]
        assert dep_related(hello, hello, max_hops=1)


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


# Additional behavioral tests from expectedBehavior


class TestActivePassiveVoice:
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("The quota exceeded the threshold", id="active_voice"),
            pytest.param("The quota was exceeded", id="passive_voice"),
        ],
    )
    def test_voice(self, text: str) -> None:
        assert nlp_scan([Clause(noun=Phrase("quota"), verb=Phrase("exceed"))], text)


class TestCopularAdjectives:
    @pytest.mark.parametrize(
        ("clause", "text"),
        [
            pytest.param(
                Clause(noun=Phrase("api"), adj=Phrase("unavailable")),
                "The API is unavailable",
                id="copular_adj",
            ),
            pytest.param(
                Clause(noun=Phrase("service"), adj=Phrase("external")),
                "An external service caused the error",
                id="attributive_adj",
            ),
        ],
    )
    def test_adj(self, clause: Clause, text: str) -> None:
        assert nlp_scan([clause], text)


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
