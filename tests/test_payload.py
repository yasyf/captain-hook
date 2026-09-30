from __future__ import annotations

import pytest

from captain_hook.util.payload import command_texts


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"cmd": "pkill -x sleep"}, ["pkill -x sleep"], id="string"),
        pytest.param(
            {"command": ["bash", "-lc", "kill -9 1234"]},
            ["bash -lc 'kill -9 1234'", "bash -lc kill -9 1234"],
            id="argv_both_renderings",
        ),
        pytest.param(
            {"command": ["echo", "hi", ";", "pkill", "-x", "sleep"]},
            ["echo hi ';' pkill -x sleep", "echo hi ; pkill -x sleep"],
            id="operator_token",
        ),
        pytest.param({"command": "kill", "args": ["-9", "14575"]}, ["kill", "-9 14575", "kill -9 14575"], id="split"),
        pytest.param({"argv": ["kill", 14575]}, ["kill 14575"], id="int_leaf"),
        pytest.param({"args": ["git", {"mode": "status"}, "reset"]}, ["git", "reset"], id="dict_item_unjoined"),
        pytest.param({"subject": "pkill", "mode": "x"}, [], id="no_carrier_key"),
    ],
)
def test_command_texts(payload: dict[str, object], expected: list[str]) -> None:
    assert list(command_texts(payload)) == expected
