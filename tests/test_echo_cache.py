from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from captain_hook.signals.nlp import PARSE_CACHE_TOKENS, parse
from captain_hook.state import RESOURCES, PrimitiveState, cached_content_lemmas


@pytest.fixture(autouse=True)
def clear_caches():
    cached_content_lemmas.cache_clear()
    parse.cache_clear()
    yield
    cached_content_lemmas.cache_clear()
    parse.cache_clear()


def test_repeated_echo_text_reuses_lemmas_without_sharing_mutable_results(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = Mock(return_value=[SimpleNamespace(lemma_="test", pos_="NOUN", is_stop=False)])
    monkeypatch.setitem(RESOURCES.__dict__, "spacy", pipeline)
    first = PrimitiveState.content_lemmas("The tests fail.")
    first.clear()
    assert PrimitiveState.content_lemmas("The tests fail.") == {"test"}
    pipeline.assert_called_once_with("The tests fail.", disable=("ner",))


def test_lemma_cache_evicts_old_text(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = Mock(return_value=[])
    monkeypatch.setitem(RESOURCES.__dict__, "spacy", pipeline)
    for i in range(129):
        PrimitiveState.content_lemmas(str(i))
    assert cached_content_lemmas.cache_info().currsize == 128
    PrimitiveState.content_lemmas("0")
    assert pipeline.call_count == 130


def test_seed_echo_reuses_signal_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    message = "Leave the codebase better than you found it."
    pipeline = Mock(return_value=MagicMock(sents=[SimpleNamespace(text=message)]))
    monkeypatch.setitem(RESOURCES.__dict__, "spacy", pipeline)
    parse(message)
    first, second = PrimitiveState(), PrimitiveState()
    first.seed_echo_verbatim(message)
    second.seed_echo_verbatim(message)
    assert first.echo_verbatim == second.echo_verbatim == [message]
    pipeline.assert_called_once_with(message)


def test_parse_cache_holds_docs_up_to_its_token_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    def doc(text: str) -> MagicMock:
        parsed = MagicMock()
        parsed.__len__.return_value = PARSE_CACHE_TOKENS // 2
        return parsed

    pipeline = Mock(side_effect=doc)
    monkeypatch.setitem(RESOURCES.__dict__, "spacy", pipeline)
    for text in ("first", "second", "third", "second", "first"):
        parse(text)
    assert [call.args[0] for call in pipeline.call_args_list] == ["first", "second", "third", "first"]
