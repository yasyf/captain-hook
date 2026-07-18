from __future__ import annotations

from pathlib import Path

import pytest

from captain_hook.util.shell import nested_command_string, plain_words, resolve_cd


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("file.txt", True, id="unquoted"),
        pytest.param(r"foo\ bar", True, id="escaped-space"),
        pytest.param("'file.txt'", False, id="single-quoted"),
        pytest.param('"file.txt"', False, id="double-quoted"),
    ],
)
def test_plain_words(raw: str, expected: bool) -> None:
    assert plain_words(raw) is expected


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        pytest.param("-lc", "rm x", id="login"),
        pytest.param("-xc", "rm x", id="trace"),
        pytest.param("-ec", "rm x", id="exit-on-error"),
        pytest.param("-ic", "rm x", id="interactive"),
        pytest.param("-c", "rm x", id="command"),
        pytest.param("-l", None, id="without-command"),
    ],
)
def test_nested_command_string_shell_flag_cluster(flag: str, expected: str | None) -> None:
    assert nested_command_string("bash", (flag, "rm x")) == expected


def test_resolve_cd_missing_returns_prior() -> None:
    prior = Path("/some/prior")
    assert resolve_cd(("/nonexistent-xyz",), prior) == prior


def test_resolve_cd_existing(tmp_path: Path) -> None:
    assert resolve_cd((str(tmp_path),), Path("/some/prior")) == tmp_path.resolve()
