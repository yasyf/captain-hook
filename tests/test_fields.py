from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from captain_hook import Deque


class Capped(BaseModel):
    xs: Deque[3]


class IntCapped(BaseModel):
    ns: Deque[int, 2]


def test_default_is_empty_bounded_deque() -> None:
    m = Capped()
    assert list(m.xs) == []
    assert m.xs.maxlen == 3


def test_auto_evicts_on_append() -> None:
    m = Capped()
    for i in range(5):
        m.xs.append(str(i))
    assert list(m.xs) == ["2", "3", "4"]


def test_maxlen_survives_roundtrip() -> None:
    m = Capped()
    for i in range(5):
        m.xs.append(str(i))
    back = Capped.model_validate_json(m.model_dump_json())
    assert list(back.xs) == ["2", "3", "4"]
    assert back.xs.maxlen == 3
    back.xs.append("z")
    assert list(back.xs) == ["3", "4", "z"]


def test_serializes_to_json_list() -> None:
    m = Capped()
    m.xs.extend(["a", "b"])
    assert json.loads(m.model_dump_json())["xs"] == ["a", "b"]


def test_element_type_set_via_subscript() -> None:
    m = IntCapped.model_validate({"ns": [1, 2, 3]})
    assert list(m.ns) == [2, 3]
    assert m.ns.maxlen == 2
    assert all(isinstance(n, int) for n in m.ns)


def test_invalid_subscript_raises() -> None:
    with pytest.raises(TypeError):
        Deque["nope"]
