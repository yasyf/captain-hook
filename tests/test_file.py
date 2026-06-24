from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from captain_hook.file import File, PathMatcher, categorize_files


class TestFileWrapsPath:
    def test_path_attribute(self) -> None:
        f = File(path=Path("bioqa/models/base.py"))
        assert f.path == Path("bioqa/models/base.py")

    def test_str_returns_string_path(self) -> None:
        assert str(File(path=Path("a/b.py"))) == "a/b.py"

    def test_fspath_for_os(self) -> None:
        f = File(path=Path("/project/foo.py"))
        assert os.fspath(f) == "/project/foo.py"


class TestFileEquality:
    def test_file_vs_file_equal(self) -> None:
        a = File(path=Path("a.py"))
        b = File(path=Path("a.py"))
        assert a == b

    def test_file_vs_path_equal(self) -> None:
        f = File(path=Path("a.py"))
        p = Path("a.py")
        assert f == p
        assert p == f

    def test_different_paths_not_equal(self) -> None:
        assert File(path=Path("a.py")) != File(path=Path("b.py"))

    def test_eq_with_string_returns_not_implemented(self) -> None:
        assert File(path=Path("a.py")).__eq__("a.py") is NotImplemented


class TestFileHashing:
    def test_hashable_in_set(self) -> None:
        s = {File(path=Path("a.py")), File(path=Path("a.py"))}
        assert len(s) == 1

    def test_hash_matches_path(self) -> None:
        f = File(path=Path("a.py"))
        assert hash(f) == hash(Path("a.py"))

    def test_usable_as_dict_key(self) -> None:
        f = File(path=Path("a.py"))
        d = {f: 1}
        assert d[File(path=Path("a.py"))] == 1


class TestFileGetattr:
    def test_nonexistent_attr_raises(self) -> None:
        f = File(path=Path("a.py"))
        with pytest.raises(AttributeError):
            f.nonexistent_attr  # type: ignore[attr-defined]


class TestFileMatches:
    @pytest.mark.parametrize(
        ("patterns", "expected"),
        [
            pytest.param(("*.py",), True, id="matches_extension"),
            pytest.param(("**/base.py",), True, id="matches_glob_pattern"),
            pytest.param(("*.ts",), False, id="no_match"),
            pytest.param(("*.rs", "*.py"), True, id="multiple_patterns_or"),
        ],
    )
    def test_matches(self, patterns: tuple[str, ...], expected: bool) -> None:
        assert File(path=Path("bioqa/models/base.py")).matches(*patterns) is expected


class TestFileUnder:
    @pytest.mark.parametrize(
        ("prefix", "expected"),
        [
            pytest.param("bioqa/", True, id="matching_prefix"),
            pytest.param("www/", False, id="non_matching_prefix"),
        ],
    )
    def test_under(self, prefix: str, expected: bool) -> None:
        assert File(path=Path("bioqa/models/base.py")).under(prefix) is expected


class TestFileIsTest:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            pytest.param("tests/test_query.py", True, id="test_prefix_file"),
            pytest.param("tests/conftest.py", True, id="conftest"),
            pytest.param("/project/bioqa/tests/util/test_helpers.py", True, id="nested_test_dir"),
            pytest.param("bioqa/main.py", False, id="non_test_file"),
        ],
    )
    def test_is_test(self, path: str, expected: bool) -> None:
        assert File(path=Path(path)).is_test is expected


class TestFileFrozen:
    def test_immutable(self) -> None:
        f = File(path=Path("a.py"))
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.path = Path("other")  # type: ignore[misc]


class TestFileExists:
    def test_exists_real_file(self, tmp_path: Path) -> None:
        p = tmp_path / "test.py"
        p.write_text("hello")
        assert File(path=p).exists()

    def test_not_exists(self) -> None:
        assert not File(path=Path("/nonexistent/file.py")).exists()


class TestFileReadText:
    def test_read_text(self, tmp_path: Path) -> None:
        p = tmp_path / "test.py"
        p.write_text("hello")
        assert File(path=p).read_text() == "hello"


class TestFileContains:
    def test_contains_pattern(self, tmp_path: Path) -> None:
        p = tmp_path / "test.py"
        p.write_text("import pdb")
        assert File(path=p).contains(r"import\s+pdb")

    def test_not_contains(self, tmp_path: Path) -> None:
        p = tmp_path / "test.py"
        p.write_text("import pdb")
        assert not File(path=p).contains(r"import\s+os")

    def test_nonexistent_file_returns_false(self) -> None:
        assert not File(path=Path("/nonexistent/file.py")).contains(r"anything")

    def test_binary_file_returns_false(self, tmp_path: Path) -> None:
        p = tmp_path / "binary"
        p.write_bytes(b"\x80\x81\x82\x83" * 100)
        assert not File(path=p).contains(r"anything")


class TestPathMatcher:
    def test_matches_string(self) -> None:
        pm = PathMatcher(patterns=["*.py", "*.ts"])
        assert pm.matches("foo.py")
        assert not pm.matches("bar.rs")

    def test_matches_path(self) -> None:
        pm = PathMatcher(patterns=["*.py"])
        assert pm.matches(Path("foo.py"))

    def test_matches_file(self) -> None:
        pm = PathMatcher(patterns=["**/test_*.py"])
        assert pm.matches(File(path=Path("tests/test_query.py")))

    def test_contains(self) -> None:
        pm = PathMatcher(patterns=["*.py"])
        assert "foo.py" in pm
        assert "foo.rs" not in pm

    def test_empty_patterns_matches_nothing(self) -> None:
        pm = PathMatcher(patterns=[])
        assert not pm.matches("test.py")


class TestCategorizeFiles:
    def test_splits_source_test_skipped(self) -> None:
        assert categorize_files(["a/test_x.py", "b/foo.py", "c/notes.md"]) == (
            ["b/foo.py"],
            ["a/test_x.py"],
            ["c/notes.md"],
        )

    def test_conftest_is_test(self) -> None:
        source, test, _ = categorize_files(["pkg/conftest.py", "pkg/foo.py"])
        assert source == ["pkg/foo.py"]
        assert test == ["pkg/conftest.py"]

    def test_files_under_tests_dir_are_test(self) -> None:
        source, test, _ = categorize_files(["pkg/tests/util/helper.py", "pkg/core.py"])
        assert source == ["pkg/core.py"]
        assert test == ["pkg/tests/util/helper.py"]

    def test_output_sorted_and_deduplicated(self) -> None:
        source, _, _ = categorize_files(["z.py", "z.py", "a.py", "b.py"])
        assert source == ["a.py", "b.py", "z.py"]

    def test_non_py_file_skipped(self) -> None:
        source, test, skipped = categorize_files(["notes.md", "ok.py"])
        assert source == ["ok.py"]
        assert test == []
        assert skipped == ["notes.md"]

    def test_blank_entries_ignored(self) -> None:
        assert categorize_files(["  ", "ok.py"]) == (["ok.py"], [], [])
