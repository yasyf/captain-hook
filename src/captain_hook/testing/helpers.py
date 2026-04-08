from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from captain_hook.conditions import matches_conditions
from captain_hook.dispatch import execute_hook
from captain_hook.testing.types import Input
from captain_hook.tests.helpers import assert_result, input_to_event
from captain_hook.types import Tool


def stub_call_llm(
    _template: Any,
    *args: Any,
    response_model: type[BaseModel] | None = None,
    **kwargs: Any,
) -> str | BaseModel:
    if response_model is None:
        return "stubbed"
    fields = response_model.model_fields
    values: dict[str, Any] = {}
    for name, info in fields.items():
        match name:
            case "block" | "fire":
                values[name] = True
            case "action":
                values[name] = "block"
            case "reasoning" | "reason":
                values[name] = "inline test stub"
            case _ if info.default is not None:
                pass
            case _:
                values[name] = ""
    return response_model(**values)


def run_inline_tests() -> list[tuple[str, str, bool, str]]:
    from captain_hook.app import _state

    results: list[tuple[str, str, bool, str]] = []

    for entry in _state.hooks:
        if not entry.spec.tests:
            continue
        for key, expected in entry.spec.tests.items():
            test_name = f"{entry.name}:{key!r}"
            try:
                if isinstance(key, Input):
                    spec_tools = [p for c in entry.spec.only_if if isinstance(c, Tool) for p in c.pattern.split("|")]
                    evt = input_to_event(
                        entry.spec.events,
                        key,
                        spec_tools[0] if spec_tools else None,
                    )
                else:
                    results.append((test_name, "skip", True, "Session keys not supported"))
                    continue

                evt.ctx.call_llm = stub_call_llm  # type: ignore[assignment]
                hook_result = execute_hook(entry, evt) if matches_conditions(entry.spec, evt) else None
                assert_result(hook_result, expected, entry.name)
                results.append((test_name, "pass", True, ""))
            except AssertionError as e:
                results.append((test_name, "fail", False, str(e)))
            except Exception as e:
                results.append((test_name, "error", False, f"{type(e).__name__}: {e}"))

    return results
