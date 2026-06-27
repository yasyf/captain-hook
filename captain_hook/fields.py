"""Reusable Pydantic field types for hook state models."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic_core import core_schema

if TYPE_CHECKING:
    from pydantic import GetCoreSchemaHandler


@dataclass(frozen=True, slots=True)
class Deque:
    """A bounded ``deque`` field type whose ``maxlen`` survives JSON round-trips.

    Subscript with the cap: ``Deque[256]`` is a ``deque[str]`` capped at 256, and
    ``Deque[int, 256]`` sets the element type. A bare ``Deque`` field (no ``= Field(...)``)
    defaults to an empty bounded deque, the cap is re-applied on every load, and the deque
    auto-evicts its oldest item on append — so a growing collection stays bounded across
    sessions. Unlike :class:`annotated_types.MaxLen`, which only rejects over-long input,
    this caps the collection instead of raising.

    Example:
        >>> from captain_hook import DurableState, Deque
        >>> class JsonShapes(DurableState, scope="global"):
        ...     shapes: Deque[256]
    """

    maxlen: int

    def __class_getitem__(cls, params: int | tuple[type, int]) -> Any:
        match params:
            case int(maxlen):
                return Annotated[deque[str], cls(maxlen)]
            case (item, int(maxlen)):
                return Annotated[deque[item], cls(maxlen)]
            case _:
                raise TypeError(f"Deque[...] takes an int cap or (item, cap), got {params!r}")

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        bounded = core_schema.no_info_after_validator_function(lambda d: deque(d, maxlen=self.maxlen), handler(source))
        return core_schema.with_default_schema(bounded, default_factory=lambda: deque(maxlen=self.maxlen))
