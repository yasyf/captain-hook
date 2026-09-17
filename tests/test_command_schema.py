from __future__ import annotations

from pathlib import Path

import pytest

from captain_hook import CommandMatches, CommandSchema, Operand, Option, OptionIs, PathMatches, PathsMatch
from captain_hook.command_schemas import FIND
from captain_hook.dispatch import dispatch
from captain_hook.loader import discover_pack
from captain_hook.packs import manager
from captain_hook.types import Event
from tests.test_cmd import evt_for


@pytest.mark.parametrize(
    ("command", "roots", "depth"),
    [
        ("find src -name /", ("src",), ()),
        ("find src / -type d", ("src", "/"), ()),
        ("find -H -f / -f src -name x", ("/", "src"), ()),
        ("find / -name x -maxdepth 2", ("/",), (2,)),
        ("find / -name -maxdepth -type f", ("/",), ()),
        ("find / -maxdepth 1 -maxdepth 99", ("/",), (1, 99)),
        (r"find / -exec echo -maxdepth 1 \;", ("/",), ()),
        (r"find / -exec echo + -maxdepth 1 \;", ("/",), ()),
        ("find / -exec echo {} + -maxdepth 1", ("/",), (1,)),
        ("find -HL / -name daemonkit", ("/",), ()),
        ("find -maxdepth 1", (".",), (1,)),
        ("find src -path / -prune -o -name x", ("src",), ()),
    ],
)
def test_expression_arguments_are_not_roots(command: str, roots: tuple[str, ...], depth: tuple[int, ...]) -> None:
    arguments = FIND.bind(evt_for(command).cmd.call("find"))
    assert arguments.values["roots"] == roots
    assert arguments.values.get("max_depth", ()) == depth
    assert arguments.complete


def test_schema_binds_a_pattern_paths_and_typed_options() -> None:
    schema = CommandSchema(
        "rg",
        operands=(Operand("pattern"), Operand("roots", count="*", default=(".",))),
        options=(Option("max_depth", ("--max-depth",), int), Option("hidden", ("--hidden",), bool)),
    )
    call = evt_for("rg --hidden error src tests --max-depth=2").cmd.call("rg")
    arguments = schema.bind(call)
    assert arguments.values == {"hidden": (True,), "max_depth": (2,), "pattern": ("error",), "roots": ("src", "tests")}
    assert arguments.words["max_depth"] == (call.command.words[-1],)
    assert arguments.words["max_depth"][0].span is not None
    assert arguments.complete


def test_variadic_sources_reserve_a_destination() -> None:
    schema = CommandSchema("cp", operands=(Operand("sources", count="*"), Operand("destination")))
    arguments = schema.bind(evt_for("cp first second /target").cmd.call("cp"))
    assert arguments.values == {"sources": ("first", "second"), "destination": ("/target",)}
    assert arguments.complete


def test_end_of_options_preserves_literal_flag_operands() -> None:
    schema = CommandSchema("cp", operands=(Operand("sources", count="*"), Operand("destination")))
    arguments = schema.bind(evt_for("cp -- -source /target").cmd.call("cp"))
    assert arguments.values == {"sources": ("-source",), "destination": ("/target",)}


@pytest.mark.parametrize("command", ["find / -maxdepth", "find / -maxdepth nope", "find / -maxdepth $DEPTH"])
def test_incomplete_bound_cannot_exempt_a_command(command: str) -> None:
    arguments = FIND.bind(evt_for(command).cmd.call("find"))
    assert not arguments.complete
    assert not OptionIs("max_depth", range(3))(arguments)


def test_unknown_option_stops_binding_without_guessing_its_value() -> None:
    arguments = FIND.bind(evt_for("find / -unknown -maxdepth 1").cmd.call("find"))
    assert not arguments.complete
    assert arguments.values["roots"] == ("/",)
    assert "max_depth" not in arguments.values


def test_unresolved_expression_cannot_establish_a_depth_exemption() -> None:
    arguments = FIND.bind(evt_for("find / -maxdepth 1 $EXPR").cmd.call("find"))
    assert not arguments.complete
    assert not OptionIs("max_depth", range(3))(arguments)


@pytest.mark.parametrize("pattern", ["$EXPR", '"$EXPR"'])
def test_unknown_option_value_cannot_establish_a_depth_exemption(pattern: str) -> None:
    arguments = FIND.bind(evt_for(f"find / -maxdepth 1 -name {pattern}").cmd.call("find"))
    assert not arguments.complete
    assert not OptionIs("max_depth", range(3))(arguments)


@pytest.mark.parametrize("roots", ["src", "-f src"])
def test_unknown_expression_preserves_known_roots(roots: str) -> None:
    arguments = FIND.bind(evt_for(f"find {roots} -newermt 2026-09-17", cwd="/repo").cmd.call("find"))
    assert not arguments.complete
    assert arguments.paths("roots").complete
    assert arguments.values["roots"] == ("src",)
    assert not PathsMatch("roots", PathMatches(("/",), unresolved=True))(arguments)


def test_substitution_keeps_missing_operands_unknown() -> None:
    arguments = FIND.bind(evt_for("find $(printf /) -name daemonkit", cwd="/repo").cmd.call("find"))
    assert not arguments.paths("roots").complete
    assert PathsMatch("roots", PathMatches(("/",), unresolved=True))(arguments)


def test_quoted_literal_stays_distinct_from_expansion() -> None:
    literal = FIND.bind(evt_for("find '$HOME' -name x", cwd="/repo").cmd.call("find"))
    expansion = FIND.bind(evt_for('find "$HOME" -name x', cwd="/repo").cmd.call("find"))
    predicate = PathsMatch("roots", PathMatches(("~",), unresolved=True))
    assert not predicate(literal)
    assert predicate(expansion)
    assert literal.paths("roots").targets[0].raw == "'$HOME'"
    assert expansion.paths("roots").targets[0].value is None


def test_path_patterns_match_segments_and_resolve_aliases(tmp_path: Path) -> None:
    (tmp_path / "root-alias").symlink_to("/")
    predicate = PathsMatch("roots", PathMatches(("/", "/Users/*")))
    assert predicate(FIND.bind(evt_for("find /Users/alice").cmd.call("find")))
    assert not predicate(FIND.bind(evt_for("find /Users/alice/project").cmd.call("find")))
    assert predicate(FIND.bind(evt_for("find root-alias", cwd=str(tmp_path)).cmd.call("find")))


def test_home_match_uses_the_same_path_normalization(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    predicate = PathsMatch("roots", PathMatches(("~",)))
    assert predicate(FIND.bind(evt_for("find ~").cmd.call("find")))
    assert not predicate(FIND.bind(evt_for("find '~'", cwd="/repo").cmd.call("find")))


def test_exemption_applies_only_to_its_own_invocation() -> None:
    condition = CommandMatches(
        FIND,
        only_if=(PathsMatch("roots", PathMatches(("/",))),),
        skip_if=(OptionIs("max_depth", range(3)),),
    )
    assert condition.check(evt_for("find / -maxdepth 1; find / -name daemonkit"))
    assert condition.check(evt_for("sh -c 'find / -name daemonkit' | head -1"))
    assert condition.check(evt_for("sudo /usr/bin/find / -name daemonkit"))
    assert condition.check(evt_for("cd / && find . -name daemonkit"))
    assert not condition.check(evt_for("echo 'find / -name daemonkit'"))


def test_performance_block_beats_general_rewrite_and_repeats(isolate_modules: None, tmp_path: Path) -> None:
    for name in ("general", "performance"):
        discover_pack(name, manager.resolve_builtin(name).path)
    for _ in range(2):
        result = dispatch(Event.PreToolUse, evt_for("find / -name daemonkit"), session_dir=tmp_path)
        assert result is not None
        output = result["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "broad filesystem search" in output["permissionDecisionReason"]
