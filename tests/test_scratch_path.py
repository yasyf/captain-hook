from __future__ import annotations

from pathlib import Path

import pytest

from captain_hook import Input, ScratchPath
from captain_hook.types import Event
from tests.helpers import input_to_event


def scratch_matches(file: str) -> bool:
    return ScratchPath().check(input_to_event(Event.PreToolUse, Input(file=file, content="x")))


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Pin the temp roots under tmp_path so the rest of tmp_path is deterministically non-scratch.
    root = (tmp_path / "tmproot").resolve()
    root.mkdir()
    monkeypatch.setattr("captain_hook.util.scratch.TEMP_ROOTS", (root,))
    return root


def test_real_file_under_temp_root_matches(temp_root: Path) -> None:
    real = temp_root / "sweep.py"
    real.write_text("x")
    assert scratch_matches(str(real)) is True


def test_real_file_under_scratch_named_dir_matches(tmp_path: Path, temp_root: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real = scratch / "plan.py"
    real.write_text("x")
    assert scratch_matches(str(real)) is True


def test_symlink_in_temp_root_to_nonscratch_target_does_not_match(tmp_path: Path, temp_root: Path) -> None:
    # .resolve() follows the symlink to the non-scratch target; parent-only resolution would wrongly
    # auto-approve a skip-permissions write through this scratch-located symlink onto the real file.
    target = tmp_path / "proj" / "main.py"
    target.parent.mkdir()
    target.write_text("x")
    link = temp_root / "link.py"
    link.symlink_to(target)
    assert scratch_matches(str(link)) is False


def test_symlink_in_scratch_named_dir_to_nonscratch_target_does_not_match(tmp_path: Path, temp_root: Path) -> None:
    target = tmp_path / "proj" / "main.py"
    target.parent.mkdir(exist_ok=True)
    target.write_text("x")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    link = scratch / "link.py"
    link.symlink_to(target)
    assert scratch_matches(str(link)) is False
