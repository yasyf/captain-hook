from __future__ import annotations

from pathlib import Path

import pytest
from cc_transcript.command import parse_command_line

from captain_hook.cmd import Cmd, Expansion, Target, Targets
from captain_hook.types import Action
from tests.helpers import make_ctx, make_post_tool_event, make_pre_tool_event


def evt_for(command: str, cwd: str | None = None):
    evt = make_pre_tool_event("Bash", {"command": command}, make_ctx())
    if cwd is not None:
        evt._raw["cwd"] = cwd
    return evt


class TestWalk:
    def test_enumerates_top_level_calls_in_order(self) -> None:
        assert [c.name for c in evt_for("rm foo.txt; ls -la; cat bar.py").cmd.calls()] == ["rm", "ls", "cat"]

    def test_call_returns_first_named_call(self) -> None:
        cmd = evt_for("ls; rm a; rm b").cmd
        assert cmd.call("rm").command.raw == "rm a"
        assert cmd.call("missing") is None

    def test_calls_filters_by_name(self) -> None:
        assert [c.command.raw for c in evt_for("rm a; ls; rm b").cmd.calls("rm")] == ["rm a", "rm b"]

    def test_descends_into_sh_c_payload(self) -> None:
        calls = evt_for("bash -c 'rm -rf /'").cmd.calls()
        assert [(c.name, c.nested, c.spliceable) for c in calls] == [("bash", False, True), ("rm", True, True)]

    def test_descends_into_eval_payload(self) -> None:
        assert [c.name for c in evt_for("eval 'rm x'").cmd.calls("rm")] == ["rm"]

    def test_surfaces_command_substitution_calls(self) -> None:
        calls = evt_for("echo $(rm sub)").cmd.calls()
        assert [(c.name, c.nested) for c in calls] == [("echo", False), ("rm", True)]

    def test_matches_command_position_only(self) -> None:
        cmd = evt_for("echo rm foo; git rm bar").cmd
        assert [c.name for c in cmd.calls()] == ["echo", "git"]
        assert cmd.calls("rm") == ()

    def test_quoted_head_does_not_evade_matching(self) -> None:
        assert evt_for('"rm" -rf /x').cmd.call("rm").name == "rm"
        assert evt_for("'rm' /x").cmd.call("rm").name == "rm"
        assert evt_for('/bin/"rm" /x').cmd.call("rm").name == "rm"

    def test_empty_and_non_bash_yield_zero_calls(self) -> None:
        assert evt_for("").cmd.calls() == ()
        assert make_pre_tool_event("Read", {"file_path": "x"}, make_ctx()).cmd.calls() == ()

    def test_calls_are_cached(self) -> None:
        cmd = evt_for("rm a; rm b").cmd
        assert cmd.calls() is cmd.calls()


class TestCallProperties:
    def test_name_is_basename_casefolded(self) -> None:
        assert evt_for("/usr/bin/RM x").cmd.call("rm").name == "rm"

    def test_wrappers_lists_stripped_wrapper_heads(self) -> None:
        call = evt_for("sudo rm /x").cmd.call("rm")
        assert call.wrappers == ("sudo",)
        assert call.name == "rm"

    def test_wrapper_value_flags_unwrap_arity_aware(self) -> None:
        call = evt_for("sudo -u root rm /x").cmd.call("rm")
        assert call.wrappers == ("sudo",)
        assert [t.value for t in call.targets] == ["/x"]
        call = evt_for("env -u HOME rm /x").cmd.call("rm")
        assert call.wrappers == ("env",)
        assert [t.value for t in call.targets] == ["/x"]

    def test_no_wrapper_yields_empty_wrappers(self) -> None:
        assert evt_for("rm /x").cmd.call("rm").wrappers == ()

    def test_top_level_occurrence_is_spliceable(self) -> None:
        assert evt_for("rm foo").cmd.call("rm").spliceable is True

    def test_spanless_payloads_are_not_spliceable(self) -> None:
        assert evt_for("echo $(rm sub)").cmd.call("rm").spliceable is False
        assert evt_for("fish -c 'rm x'").cmd.call("rm").spliceable is False

    def test_cwd_threads_through_resolvable_cd(self) -> None:
        assert [(c.name, str(c.cwd)) for c in evt_for("cd /tmp && rm x", cwd="/home/u").cmd.calls()] == [
            ("cd", "/home/u"),
            ("rm", str(Path("/tmp").resolve())),
        ]

    def test_piped_cd_does_not_thread_cwd(self) -> None:
        assert [(c.name, str(c.cwd)) for c in evt_for("cd /tmp | rm x", cwd="/home/u").cmd.calls()] == [
            ("cd", "/home/u"),
            ("rm", "/home/u"),
        ]

    def test_cd_threads_within_payload_scope(self) -> None:
        cmd = evt_for('sh -c "cd /tmp && rm x" && rm y', cwd="/home/u").cmd
        nested, top = cmd.calls("rm")
        assert str(nested.cwd) == str(Path("/tmp").resolve())
        assert str(top.cwd) == "/home/u"


class TestVerified:
    @pytest.mark.parametrize("command", ["rm $HOME", 'rm "$V"/x'], ids=["bare-var", "quoted-var"])
    def test_expansion_tainted_target_is_unverified(self, command: str) -> None:
        (target,) = evt_for(command).cmd.call("rm").targets
        assert target.verified is False
        assert target.value is None
        assert target.path is None

    @pytest.mark.parametrize("command", ["rm ~/x", "rm *.py"], ids=["tilde", "glob"])
    def test_expandable_literal_target_stays_verified(self, command: str) -> None:
        (target,) = evt_for(command).cmd.call("rm").targets
        assert target.verified is True
        assert target.value is not None

    def test_in_word_substitution_taints_the_target(self) -> None:
        (first, second) = evt_for("rm a$(b)c x").cmd.call("rm").targets
        assert first.verified is False
        assert second.verified is True

    def test_lifted_substitution_marks_targets_incomplete(self) -> None:
        targets = evt_for("rm $(ls) b").cmd.call("rm").targets
        assert [t.value for t in targets] == ["b"]
        assert targets.complete is False
        assert targets.verified is False
        assert targets.expand().exhausted is True

    def test_verified_targets_are_complete(self) -> None:
        targets = evt_for("rm a b").cmd.call("rm").targets
        assert targets.complete is True
        assert targets.verified is True

    def test_unverified_target_expands_exhausted(self) -> None:
        target = Target(None, "$HOME", None)
        assert target.expand() == Expansion((), True)
        assert target.has_glob is False


class TestTarget:
    def test_path_resolves_against_cwd_keeping_final_component_literal(self, tmp_path: Path) -> None:
        assert Target("sub/file", "sub/file", tmp_path).path == tmp_path / "sub" / "file"

    def test_relative_target_without_cwd_has_no_path(self) -> None:
        target = Target("file", "file", None)
        assert target.path is None
        assert target.is_scratch is target.is_repo_root is target.in_repo is False
        assert target.is_fs_root is target.is_home is target.contains_repo is False

    def test_unverified_target_has_no_path_and_no_predicates(self) -> None:
        target = Target(None, "$HOME", Path("/tmp"))
        assert target.path is None
        assert target.is_scratch is target.is_home is target.is_fs_root is False

    def test_has_glob(self) -> None:
        assert Target("*.py", "*.py", None).has_glob is True
        assert Target("plain.py", "plain.py", None).has_glob is False

    def test_is_scratch_under_temp_root(self) -> None:
        assert Target("x", "x", Path("/tmp")).is_scratch is True

    def test_is_repo_root(self, tmp_path: Path) -> None:
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        assert Target("repo", "repo", tmp_path).is_repo_root is True
        assert Target("repo", "repo", tmp_path).in_repo is True

    def test_is_fs_root(self) -> None:
        assert Target("/", "/", None).is_fs_root is True
        assert Target("/etc", "/etc", None).is_fs_root is False

    def test_is_home_for_top_level_users_entry(self) -> None:
        assert Target("/Users/alice", "/Users/alice", None).is_home is True

    def test_contains_repo(self, tmp_path: Path) -> None:
        (tmp_path / "outer" / "inner" / ".git").mkdir(parents=True)
        assert Target("outer", "outer", tmp_path).contains_repo is True
        (tmp_path / "empty").mkdir()
        assert Target("empty", "empty", tmp_path).contains_repo is False

    def test_expand_literal_yields_itself(self) -> None:
        assert list(Target("plain.txt", "plain.txt", None).expand()) == ["plain.txt"]

    def test_expand_glob_matches(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.txt").touch()
        (tmp_path / "c.log").touch()
        assert sorted(Target("*.txt", "*.txt", tmp_path).expand()) == ["a.txt", "b.txt"]

    def test_expand_caps_at_limit_plus_one(self, tmp_path: Path) -> None:
        for i in range(12):
            (tmp_path / f"f{i}.txt").touch()
        assert len(Target("*.txt", "*.txt", tmp_path).expand(limit=10)) == 11


class TestExpansionAndTargets:
    def test_expansion_len_iter_bool(self) -> None:
        expansion = Expansion(("a", "b"), False)
        assert len(expansion) == 2
        assert list(expansion) == ["a", "b"]
        assert bool(expansion) is True
        assert bool(Expansion((), False)) is False

    def test_empty_targets(self) -> None:
        targets = Targets()
        assert len(targets) == 0
        assert list(targets) == []
        assert targets.expand() == Expansion((), False)

    def test_incomplete_targets_expand_exhausted(self) -> None:
        assert Targets((), complete=False).expand() == Expansion((), True)

    def test_targets_expand_concatenates_and_ors_exhausted(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").touch()
        targets = Targets((Target("literal", "literal", tmp_path), Target("*.txt", "*.txt", tmp_path)))
        expansion = targets.expand()
        assert sorted(expansion.matches) == ["a.txt", "literal"]
        assert expansion.exhausted is False


class TestBool:
    def test_empty_command_is_falsy_and_short_circuits(self) -> None:
        evt = evt_for("")
        assert bool(evt.command) is False
        assert (evt.command or "x") == "x"

    def test_nonempty_command_is_truthy(self) -> None:
        evt = evt_for("rm foo")
        assert bool(evt.command) is True
        assert (evt.command or "x") is evt.command

    def test_comment_line_is_truthy_cmd_with_falsy_line(self) -> None:
        cmd = Cmd.parse("# rm -rf /tmp/x")
        assert bool(cmd) is True
        assert bool(cmd.line) is False


class TestDetachedAndSubGuards:
    def test_parse_builds_detached_cmd(self) -> None:
        cmd = Cmd.parse("rm foo; ls")
        assert [c.name for c in cmd.calls()] == ["rm", "ls"]
        assert cmd.event is None

    def test_cmd_over_prebuilt_command_line(self) -> None:
        cmd = Cmd(parse_command_line("rm a; rm b"))
        assert [c.command.raw for c in cmd.calls("rm")] == ["rm a", "rm b"]

    def test_sub_wrong_old_raises_value_error(self) -> None:
        call = evt_for("rm foo").cmd.call("rm")
        with pytest.raises(ValueError, match="must match the call name"):
            call.sub("trash", "rm")

    def test_sub_on_detached_cmd_raises_runtime_error(self) -> None:
        call = Cmd.parse("rm foo").call("rm")
        with pytest.raises(RuntimeError, match="rewrite-capable event"):
            call.sub("rm", "trash")

    def test_sub_on_non_rewrite_event_raises_runtime_error(self) -> None:
        call = make_post_tool_event("Bash", {"command": "rm foo"}).cmd.call("rm")
        with pytest.raises(RuntimeError, match="rewrite-capable event"):
            call.sub("rm", "trash")

    def test_sub_on_substituted_call_returns_none(self) -> None:
        assert evt_for("rm $(ls) b").cmd.call("rm").sub("rm", "trash") is None

    def test_sub_on_spanless_payload_returns_none(self) -> None:
        assert evt_for("fish -c 'rm x'").cmd.call("rm").sub("rm", "trash") is None

    def test_sub_on_unembeddable_replacement_returns_none(self) -> None:
        call = evt_for("eval 'rm a; rm b'").cmd.calls("rm")[0]
        assert call.sub("rm", "trash", args=Targets((Target("a b", "'a b'", None),))) is None


class TestSplitOptions:
    def test_flags_and_targets_partition_args(self) -> None:
        call = evt_for("rm -rf foo bar").cmd.call("rm")
        assert call.flags == ("-rf",)
        assert [t.value for t in call.targets] == ["foo", "bar"]

    def test_double_dash_ends_options(self) -> None:
        call = evt_for("rm -f -- -weird.txt").cmd.call("rm")
        assert call.flags == ("-f",)
        assert [t.value for t in call.targets] == ["-weird.txt"]

    def test_lone_dash_is_an_operand(self) -> None:
        call = evt_for("rm -").cmd.call("rm")
        assert [t.value for t in call.targets] == ["-"]

    def test_quoted_operand_dequotes_in_value_keeping_raw(self) -> None:
        (target,) = evt_for("rm 'a b.txt'").cmd.call("rm").targets
        assert target.value == "a b.txt"
        assert target.raw == "'a b.txt'"

    def test_git_value_flag_consumes_its_argument(self) -> None:
        call = evt_for("git -C /repo status").cmd.call("git")
        assert call.flags == ("-C", "/repo")
        assert [t.value for t in call.targets] == ["status"]

    def test_equals_joined_flag_stays_single_token(self) -> None:
        call = evt_for("git --git-dir=/g status").cmd.call("git")
        assert call.flags == ("--git-dir=/g",)
        assert [t.value for t in call.targets] == ["status"]


class TestSub:
    def test_rewrites_one_occurrence_preserving_siblings(self) -> None:
        evt = evt_for("rm foo.txt; ls -la; rm bar.txt")
        result = None
        for call in evt.cmd.calls("rm"):
            result = call.sub("rm", "trash", args=call.targets)
        assert result.action == Action.rewrite
        assert result.updated_input == {"command": "trash foo.txt; ls -la; trash bar.txt"}

    def test_default_args_reemit_full_arguments(self) -> None:
        evt = evt_for("rm -rf foo")
        result = evt.cmd.call("rm").sub("rm", "trash")
        assert result.updated_input == {"command": "trash -rf foo"}

    def test_args_targets_drops_flags(self) -> None:
        evt = evt_for("rm -rf foo")
        call = evt.cmd.call("rm")
        result = call.sub("rm", "trash", args=call.targets)
        assert result.updated_input == {"command": "trash foo"}

    def test_dash_leading_raw_gets_dot_slash_prefix(self) -> None:
        evt = evt_for("rm -- -foo.txt")
        call = evt.cmd.call("rm")
        result = call.sub("rm", "trash", args=call.targets)
        assert result.updated_input == {"command": "trash ./-foo.txt"}

    def test_wrappers_drop_on_rewrite(self) -> None:
        evt = evt_for("sudo rm /x")
        call = evt.cmd.call("rm")
        result = call.sub("rm", "trash", args=call.targets)
        assert result.updated_input == {"command": "trash /x"}

    def test_continuation_collapses_on_rewrite(self) -> None:
        evt = evt_for("rm a \\\n b && rm c")
        result = evt.cmd.calls("rm")[0].sub("rm", "trash")
        assert result.updated_input == {"command": "trash a b && rm c"}

    def test_splices_nested_payload_through_quote_layers(self) -> None:
        evt = evt_for('sh -c "cd /tmp && rm -rf y" && rm z')
        result = evt.cmd.calls("rm")[0].sub("rm", "trash")
        assert result.updated_input == {"command": 'sh -c "cd /tmp && trash -rf y" && rm z'}

    def test_accumulated_subs_rewrite_every_occurrence(self) -> None:
        evt = evt_for("rm a && rm b")
        result = None
        for call in evt.cmd.calls("rm"):
            result = call.sub("rm", "trash", args=call.targets, note="moved to trash")
        assert result.updated_input == {"command": "trash a && trash b"}
        assert result.note == "moved to trash"

    def test_returning_block_discards_accumulated_subs(self) -> None:
        evt = evt_for("rm a; git push")
        result = None
        for call in evt.cmd.calls():
            if call.name == "rm":
                result = call.sub("rm", "trash", args=call.targets)
            elif call.name == "git":
                result = evt.block("Pushing is disabled")
                break
        assert result.action == Action.block
        assert result.updated_input is None

    def test_reemits_verbatim_source_spelling(self) -> None:
        evt = evt_for("rm 'a b.txt'")
        call = evt.cmd.call("rm")
        result = call.sub("rm", "trash", args=call.targets)
        assert result.updated_input == {"command": "trash 'a b.txt'"}
