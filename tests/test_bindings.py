from __future__ import annotations

from pathlib import Path

import pytest

from captain_hook.bindings import Resolved, Scope, Unresolved
from captain_hook.cmd import Target
from tests.test_cmd import evt_for


def resolve(text: str, word: str) -> Resolved | Unresolved:
    return Scope(text).resolve(word, text.rfind(word))


def candidates(text: str, word: str) -> tuple[str, ...] | None:
    match resolve(text, word):
        case Resolved(values, _):
            return values
        case Unresolved(_):
            return None


class TestKnownBindings:
    @pytest.mark.parametrize(
        ("text", "word", "expected"),
        [
            ("S=~/.claude/scratch; $S/run.sh", "$S/run.sh", ("~/.claude/scratch/run.sh",)),
            ('AB="agent-browser --session x"; $AB open', "$AB", ("agent-browser --session x",)),
            ("export X=val; $X", "$X", ("val",)),
            ("x=1 y=2; echo $x", "$x", ("1",)),
            ("p=ls; p=pkill; $p", "$p", ("pkill",)),
            ("p=ls; p=; $p kill 1", "$p", ("",)),
            ("for v in 0.2.9 0.2.11; do find $v; done", "$v", ("0.2.9", "0.2.11")),
            ("cd /x && for d in a b; do find $d; done", "$d", ("a", "b")),
            ("ITEMS='echo pkill'; for X in $ITEMS; do $X; done", "$X", ("echo", "pkill")),
            ("$HOME/.claude/bin/ledger.py --help", "$HOME/.claude/bin/ledger.py", ("~/.claude/bin/ledger.py",)),
            ("X=a; printf '%s' \"$X\\q\"", '"$X\\q"', ("a\\q",)),
            ("X=p\\\nkill; $X", "$X", ("pkill",)),
            ("X=pkill; cat <<EOF\n\tEOF\nX=echo\nEOF\n$X", "$X", ("pkill",)),
            ("X=pkill; cat <<-EOF\n\tEOF\nX=echo\nEOF\n$X", "$X", ("echo",)),
            ("X=echo; f() { echo }; $X claude; }; X=pkill; f", "$X claude", None),
            ("X=pkill; X=echo >/dev/null true; $X", "$X", ("pkill",)),
            ("X=echo; R=echo; $R X; $X", "$X", ("echo",)),
            ("X=echo; S=~/.claude/scratch; $S/run.sh X; $X", "$X", ("echo",)),
        ],
    )
    def test_resolves_literal_unconditional_assignments(
        self, text: str, word: str, expected: tuple[str, ...] | None
    ) -> None:
        assert candidates(text, word) == expected


class TestUnknownBindings:
    @pytest.mark.parametrize(
        ("text", "word"),
        [
            ("P=$(ls | head -1); $P", "$P"),
            ("read p; $p", "$p"),
            ("read 'X' <<<pkill; $X", "$X"),
            ("REPLY=echo; read <<<pkill; $REPLY", "$REPLY"),
            ("X=echo; read 'X[0]' <<<pkill; $X", "$X"),
            ("X=echo; printf -v X pkill; $X", "$X"),
            ("X=echo; printf -vX '%s' pkill; $X", "$X"),
            ("X=echo; 'read' X <<<pkill; $X", "$X"),
            ("X=echo; builtin read X <<<pkill; $X", "$X"),
            ("X=echo; ! read X <<<pkill; $X", "$X"),
            ("X=echo; time read X <<<pkill; $X", "$X"),
            ("X=echo; X[0]=pkill; $X", "$X"),
            ("X=echo; X=(pkill); $X", "$X"),
            ("X=<(printf x); $X", "$X"),
            ("X=pk; X+=ill; $X", "$X"),
            ("X=echo; declare -n Y=X; Y=pkill; $X", "$X"),
            ("X=0; declare -i X; X=1+2; $X", "$X"),
            ("X=echo; eval 'X=pkill'; $X; source /dev/null", "$X"),
            ("eval 'p=ls'; $p", "$p"),
            ("case $x in a) p=1;; esac; $p", "$p"),
            ("(p=ls); $p", "$p"),
            ("if true; then p=ls; fi; $p", "$p"),
            ("false && p=ls; $p", "$p"),
            ("X=pkill; X=echo | cat; $X", "$X"),
            ("X=pkill; X=echo & $X", "$X"),
            ("X=pkill; X=echo &>/dev/null | cat; $X", "$X"),
            ('X=; : "${X:=pkill}"; $X', "$X"),
            ('X=1; : "$((X=2))"; $X', "$X"),
            ("X=1; ((X++)); $X", "$X"),
            ("X=1; let '++X'; $X", "$X"),
            ("f() { CMD=ls; }; $CMD 1; CMD=kill; f", "$CMD 1"),
            ("function g { X=1; }; X=2; $X", "$X"),
            ('X=echo; function f() { "$X" claude; }; X=pkill; f', '"$X" claude'),
            ("while read -r t; do $t; done < f", "$t"),
            ("for d in a b; do :; done; $d", "$d"),
            ("for X in {echo,pkill}; do $X; done", "$X"),
            ("X=pkill; Y=$(echo ok # )\nX=echo\n); $X", "$X"),
            ("X=pkill; Y=$(case x in x) :; X=echo;; esac); $X", "$X"),
            ("X=pkill; Y=$(cat <<EOF\n)\nX=echo\nEOF\n); $X", "$X"),
            ("PWD=/tmp; cd /; rm -rf $PWD", "$PWD"),
            ("IFS=:; X='/tmp:/'; rm -rf $X", "$X"),
            ("X='a b'; printf '<%s>' \"a b\"$X", '"a b"$X'),
            ("X=1 2>/dev/null; $X", "$X"),
            ("set -- a b; $1", "$1"),
            ("X=${Y:-z}; $X", "$X"),
            ("for X in *.sh; do $X; done", "$X"),
            ("for P in /usr/bin/[p]kill; do $P; done", "$P"),
            ('X=echo; V=X; read "$V" <<<pkill; $X', "$X"),
            ('X=echo; V=X; printf -v "$V" pkill; $X', "$X"),
            ("X=echo; V=X; unset $V; $X", "$X"),
            ("X=echo; R=read; $R X <<<pkill; $X", "$X"),
            ("X=echo; R=$(echo read); $R X <<<pkill; $X", "$X"),
            ("X=echo; $UNBOUND X; $X", "$X"),
            ("X=echo; read IFS <<<:; $X", "$X"),
            ("X=echo; export IFS=:; $X", "$X"),
            ("X=echo; unset IFS; $X", "$X"),
            ("X=echo; for IFS in :; do :; done; $X", "$X"),
            ('X=echo; : "${IFS:=:}"; $X', "$X"),
        ],
    )
    def test_stays_unresolved(self, text: str, word: str) -> None:
        assert isinstance(resolve(text, word), Unresolved)

    def test_unbound_name_reads_as_environment(self) -> None:
        assert resolve("$EDITOR notes.md", "$EDITOR") == Unresolved("")

    def test_substitution_source_is_the_text_feeding_it(self) -> None:
        assert resolve("B=$(realpath ../bin/biome); $B", "$B") == Unresolved("$(realpath ../bin/biome)")

    def test_an_unreadable_binder_keeps_its_earliest_offset(self) -> None:
        scope = Scope("X=echo; eval 'X=pkill'; $X claude; source /dev/null")
        assert scope.unreadable_from == len("X=echo; eval 'X=pkill'")


class TestLimits:
    def test_repeated_references_share_one_choice(self) -> None:
        text = "for x in a b; do echo " + "$x" * 40 + "; done"
        assert candidates(text, "$x" * 40) == ("a" * 40, "b" * 40)

    def test_candidate_growth_is_bounded(self) -> None:
        text = "X=a; " + "X=$X$X; " * 32 + "printf '%s' $X"
        assert isinstance(resolve(text, "$X"), Unresolved)

    def test_too_many_combinations_stay_unresolved(self) -> None:
        text = "for a in 1 2 3 4 5 6 7 8 9; do for b in 1 2 3 4 5 6 7 8 9; do echo $a$b; done; done"
        assert isinstance(resolve(text, "$a$b"), Unresolved)

    def test_a_long_literal_list_resolves(self) -> None:
        text = "for d in " + " ".join(f"d{n}" for n in range(20)) + "; do find $d -name '*.go'; done | head"
        assert candidates(text, "$d") == tuple(f"d{n}" for n in range(20))

    @pytest.mark.parametrize("text", ['X="$(' * 600, "X='" + "(" * 5000, "'" * 3, "\x00$X\x00", "é" * 10 + "$X"])
    def test_pathological_text_never_raises(self, text: str) -> None:
        evt_for(text).cmd.scope


class TestCallResolution:
    def test_byte_spans_map_to_character_offsets(self) -> None:
        evt = evt_for(': éééééééééééééééééééé; X=/; rm "$X"; X=/tmp')
        (target,) = evt.cmd.call("rm").targets
        assert target.value == "/"

    def test_unquoted_candidates_split_into_targets(self) -> None:
        evt = evt_for("X='/tmp/x /'; rm -rf $X", cwd="/")
        assert [target.value for target in evt.cmd.call("rm").targets] == ["/tmp/x", "/"]

    def test_any_unquoted_occurrence_makes_the_word_splittable(self) -> None:
        evt = evt_for('X="/tmp/a /Users/yasyf /tmp/b"; rm -rf $X"$X"', cwd="/")
        assert all(target.value is None for target in evt.cmd.call("rm").targets)

    def test_escaped_dollar_in_a_double_quoted_payload_never_resolves(self) -> None:
        evt = evt_for('P=/tmp; sh -c "P=/; rm -rf \\$P"', cwd="/")
        (target,) = evt.cmd.calls("rm")[0].targets
        assert target.value is None

    def test_relative_candidate_needs_a_cwd(self) -> None:
        (target,) = evt_for("X=src; rm -rf $X").cmd.call("rm").targets
        assert target == Target(None, "$X", None)

    def test_double_quoted_payload_resolves_in_the_host_scope(self) -> None:
        evt = evt_for('X=/; sh -c "X=/tmp; rm -rf $X"', cwd="/")
        (target,) = evt.cmd.calls("rm")[0].targets
        assert target.value == "/"

    def test_single_quoted_payload_sees_only_exports_and_its_own_assignments(self) -> None:
        evt = evt_for("X=/tmp; export Y=/tmp; sh -c 'rm -rf $X; rm -rf $Y; Z=/; rm -rf $Z'", cwd="/")
        values = [target.value for call in evt.cmd.calls("rm") for target in call.targets]
        assert values == [None, "/tmp", "/"]

    def test_substitution_body_assignments_apply_inside_it(self) -> None:
        evt = evt_for('X=/tmp; echo "pre$(X=/; rm -rf "$X")post"', cwd="/")
        (target,) = evt.cmd.calls("rm")[0].targets
        assert target.value == "/"

    def test_host_environment_prefix_reaches_the_shell_child(self) -> None:
        evt = evt_for("export X=/tmp; X=/ sh -c 'rm -rf \"$X\"'", cwd="/")
        (target,) = evt.cmd.calls("rm")[0].targets
        assert target.value == "/"

    def test_export_minus_n_unexports(self) -> None:
        evt = evt_for("export X=/tmp; export -n X; sh -c 'rm -rf \"$X/\"'", cwd="/")
        (target,) = evt.cmd.calls("rm")[0].targets
        assert target.value is None

    def test_export_attribute_survives_reassignment(self) -> None:
        evt = evt_for("export X; X=/; sh -c 'rm -rf $X'", cwd="/")
        (target,) = evt.cmd.calls("rm")[0].targets
        assert target.value == "/"

    def test_payload_inside_a_function_never_resolves(self) -> None:
        evt = evt_for("export X=/tmp; f() { sh -c 'rm -rf \"$X\"'; }; X=/; f", cwd="/")
        (target,) = evt.cmd.calls("rm")[0].targets
        assert target.value is None

    def test_joined_eval_payload_with_repeated_commands_never_resolves(self) -> None:
        evt = evt_for("X=/tmp; eval 'rm -rf \"$X\"; X=/; rm -rf \"$X\"' ''", cwd="/")
        assert all(target.value is None for call in evt.cmd.calls("rm") for target in call.targets)

    def test_empty_cd_operand_loses_the_cwd(self) -> None:
        evt = evt_for("cd /tmp; D=''; cd $D; rm -rf Documents")
        assert evt.cmd.calls("rm")[0].cwd is None

    def test_variable_cd_threads_the_cwd(self) -> None:
        evt = evt_for("R=/tmp; cd $R && rm -rf x")
        assert str(evt.cmd.calls("rm")[0].cwd) == str(Path("/tmp").resolve())

    def test_nested_variable_cd_does_not_recurse(self) -> None:
        evt = evt_for("export X=/tmp; sh -c 'cd \"$X\"; rm -rf child'")
        assert evt.cmd.calls("rm")
