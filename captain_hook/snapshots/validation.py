from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from math import isfinite
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


@lru_cache(maxsize=7)
def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((Path(__file__).with_name("schemas") / f"{name}.schema.json").read_text()))


def validate(name: str, value: object) -> None:
    validator(name).validate(value)


def canonical_numbers(value: Any) -> Any:
    match value:
        case dict():
            return {key: canonical_numbers(item) for key, item in value.items()}
        case list():
            return [canonical_numbers(item) for item in value]
        case Decimal():
            if not value:
                return 0
            magnitude = float(value)
            if not isfinite(magnitude) or magnitude == 0:
                raise ValueError("snapshot number exceeds finite nonzero binary64 magnitude")
            return int(value) if value.copy_abs() <= 2**53 - 1 and value == value.to_integral_value() else value
        case float():
            if not isfinite(value):
                raise ValueError("snapshot number must be finite")
            return int(value) if value.is_integer() and abs(value) <= 2**53 - 1 else value
        case _:
            return value


def finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def parse_exact(payload: bytes) -> Any:
    return json.loads(payload, parse_float=Decimal, parse_constant=finite_constant)


def native_numbers(value: Any) -> Any:
    match value:
        case dict():
            return {key: native_numbers(item) for key, item in value.items()}
        case list():
            return [native_numbers(item) for item in value]
        case Decimal():
            return float(value)
        case _:
            return value


def checked(name: str, value: object) -> Any:
    precise = canonical_numbers(value)
    validate(name, precise)
    return native_numbers(precise)
