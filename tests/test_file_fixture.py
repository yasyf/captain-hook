from __future__ import annotations

import os
from pathlib import Path

import pytest
from cc_transcript.tools import ReadCall

from captain_hook.testing import FileFixture, Input
from captain_hook.testing.helpers import input_to_event, materialize_file
from captain_hook.types import Event


def test_file_fixture_size_materializes_real_file() -> None:
    path = materialize_file(FileFixture(size=12345))
    assert os.path.getsize(path) == 12345


def test_file_fixture_content_writes_bytes() -> None:
    path = materialize_file(FileFixture(content="abc"))
    with open(path, "rb") as f:
        assert f.read() == b"abc"


def test_file_fixture_name_is_honored() -> None:
    path = materialize_file(FileFixture(content="hi", name="readme.txt"))
    assert os.path.basename(path) == "readme.txt"


def test_read_event_from_file_fixture() -> None:
    evt = input_to_event(Event.PreToolUse, Input(tool="Read", file=FileFixture(size=12345)))
    assert evt.file is not None
    assert evt.file.path.exists()
    assert evt.file.path.stat().st_size == 12345
    assert isinstance(evt.input, ReadCall)
    assert evt.input.offset is None
    assert evt.input.limit is None


def test_read_event_carries_offset_and_limit() -> None:
    evt = input_to_event(Event.PreToolUse, Input(tool="Read", file="x.py", offset=10, limit=50))
    assert isinstance(evt.input, ReadCall)
    assert evt.input.offset == 10
    assert evt.input.limit == 50
    assert evt._raw["tool_input"]["offset"] == 10
    assert evt._raw["tool_input"]["limit"] == 50


def test_home_fixture_without_name_raises() -> None:
    with pytest.raises(ValueError, match="requires name"):
        FileFixture(home=True)


def test_home_fixture_on_non_tool_event_is_inert() -> None:
    evt = input_to_event(
        Event.UserPromptSubmit, Input(prompt="hi", file=FileFixture(home=True, name="secret", content="x"))
    )
    assert "_home_dir" not in evt.__dict__


def test_home_fixture_materializes_under_its_own_directory() -> None:
    path = materialize_file(FileFixture(home=True, name="cfg", content="x"))
    assert Path(path).name == "cfg"
    with open(path) as f:
        assert f.read() == "x"


def test_two_home_fixtures_materialize_to_different_directories() -> None:
    p1 = materialize_file(FileFixture(home=True, name="cfg", content="a"))
    p2 = materialize_file(FileFixture(home=True, name="cfg", content="b"))
    assert Path(p1).parent != Path(p2).parent


def test_file_token_substituted_when_fixture_present() -> None:
    evt = input_to_event(Event.PreToolUse, Input(tool="Bash", command="cat {file}", file=FileFixture(content="hi")))
    assert "{file}" not in evt.command.raw
    assert evt.command.raw.startswith("cat ")
    materialized = evt.command.raw.removeprefix("cat ")
    assert os.path.exists(materialized)
    with open(materialized) as f:
        assert f.read() == "hi"


def test_file_token_left_alone_without_fixture() -> None:
    evt = input_to_event(Event.PreToolUse, Input(tool="Bash", command="cat {file}", file="plain/path.txt"))
    assert evt.command.raw == "cat {file}"


def test_file_token_left_alone_without_the_token() -> None:
    evt = input_to_event(Event.PreToolUse, Input(tool="Bash", command="cat foo.py", file=FileFixture(content="hi")))
    assert evt.command.raw == "cat foo.py"
