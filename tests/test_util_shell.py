from __future__ import annotations

from pathlib import Path

import pytest

from captain_hook.util.shell import plain_words, resolve_cd


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


def test_resolve_cd_missing_returns_prior() -> None:
    prior = Path("/some/prior")
    assert resolve_cd(("/nonexistent-xyz",), prior) == prior


def test_resolve_cd_existing(tmp_path: Path) -> None:
    assert resolve_cd((str(tmp_path),), Path("/some/prior")) == tmp_path.resolve()
