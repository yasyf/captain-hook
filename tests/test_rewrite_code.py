from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from captain_hook import ast_grep, rewrite_code, rewrite_command
from captain_hook.command import CommandLine
from captain_hook.dispatch import dispatch
from captain_hook.events import PreToolUseEvent
from captain_hook.types import Event
from tests.helpers import build_ctx


def pre_event(tool: str, tool_input: dict[str, Any], *, project_root: Path | None = None) -> PreToolUseEvent:
    return PreToolUseEvent(_raw={"tool_name": tool, "tool_input": tool_input}, ctx=build_ctx(project_root=project_root))


def edit(new: str, file: str = "deploy.py") -> PreToolUseEvent:
    return pre_event("Edit", {"file_path": file, "old_string": "", "new_string": new})


def bash(command: str) -> PreToolUseEvent:
    return pre_event("Bash", {"command": command})


def updated_input(result: dict[str, Any] | None) -> dict[str, Any]:
    assert result is not None
    return result["hookSpecificOutput"]["updatedInput"]


class TestRewriteCore:
    @pytest.mark.parametrize(
        ("code", "lang", "pattern", "replacement", "expected"),
        [
            pytest.param(
                "os.system(cmd)\n", "py", "os.system($CMD)", "run([$CMD])", "run([cmd])\n", id="single_metavar"
            ),
            pytest.param(
                "print(a,   b)\n",
                "py",
                "print($$$A)",
                "log($$$A)",
                "log(a,   b)\n",
                id="triple_metavar_preserves_whitespace",
            ),
            pytest.param("x = 1\n", "py", "print($$$)", "log()", "x = 1\n", id="no_match_passthrough"),
            pytest.param("print()\n", "py", "print($$$A)", "log($$$A)", "log()\n", id="empty_triple_metavar"),
            pytest.param(
                "console.log(x)\n", "ts", "console.log($A)", "logger.info($A)", "logger.info(x)\n", id="typescript"
            ),
        ],
    )
    def test_rewrite(self, code: str, lang: str, pattern: str, replacement: str, expected: str) -> None:
        assert ast_grep.rewrite(code, lang, pattern, replacement) == expected

    def test_uncaptured_metavar_stays_literal(self) -> None:
        # A $NAME the pattern never captured passes through untouched (mismatched names, shell vars).
        assert ast_grep.rewrite("foo(123)\n", "py", "foo($X)", "bar($Y)") == "bar($Y)\n"
        shell = ast_grep.rewrite("cat a.txt\n", "bash", "cat $$$A", "bat $$$A && echo $HOME")
        assert shell == "bat a.txt && echo $HOME\n"

    def test_bash_lang_inferred_from_extension(self) -> None:
        assert ast_grep.lang_for_path(Path("deploy.sh")) == "bash"
        assert ast_grep.lang_for_path(Path("lib.bash")) == "bash"

    def test_has_metavar(self) -> None:
        assert ast_grep.has_metavar("cat $$$ARGS")
        assert ast_grep.has_metavar("os.system($CMD)")
        assert not ast_grep.has_metavar(r"^cat\s+(\S+)$")


class TestCapture:
    @pytest.mark.parametrize(
        ("raw", "lang", "pattern", "expected"),
        [
            pytest.param(
                "sed -n 10,40p f.go", "bash", "sed -n $R $F", {"R": "10,40p", "F": "f.go"}, id="named_singles"
            ),
            pytest.param(
                "sed -n '10,40p' f.go",
                "bash",
                "sed -n $R $F",
                {"R": "'10,40p'", "F": "f.go"},
                id="quotes_preserved_from_raw",
            ),
            pytest.param(
                "cat a.txt b.txt c.txt",
                "bash",
                "cat $$$FILES",
                {"FILES": "a.txt b.txt c.txt"},
                id="triple_metavar_span",
            ),
            pytest.param(
                "echo  a   b", "bash", "echo $$$ARGS", {"ARGS": "a   b"}, id="triple_metavar_preserves_whitespace"
            ),
            pytest.param(
                "git commit -m hi",
                "bash",
                "git $SUB $$$REST",
                {"SUB": "commit", "REST": "-m hi"},
                id="mixed_single_and_triple",
            ),
            pytest.param("ls -la", "bash", "ls -la", {}, id="no_metavar_match_is_empty_dict"),
            pytest.param("ls -la", "bash", "sed -n $R $F", None, id="no_match_is_none"),
        ],
    )
    def test_capture(self, raw: str, lang: str, pattern: str, expected: dict[str, str] | None) -> None:
        assert ast_grep.capture(raw, lang, pattern) == expected


class TestRewriteCode:
    def test_edit_rewrites_new_string(self) -> None:
        rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)", note="use subprocess")
        out = updated_input(dispatch(Event.PreToolUse, edit('os.system("ls")\n')))
        assert out["new_string"] == 'subprocess.run(["ls"], check=True)\n'

    def test_note_surfaces_as_context(self) -> None:
        rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)", note="use subprocess")
        result = dispatch(Event.PreToolUse, edit('os.system("ls")\n'))
        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "use subprocess"

    def test_no_match_passes_through(self) -> None:
        rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)")
        assert dispatch(Event.PreToolUse, edit("x = 1\n")) is None

    def test_idempotent_on_already_rewritten(self) -> None:
        rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)")
        assert dispatch(Event.PreToolUse, edit('subprocess.run(["ls"], check=True)\n')) is None

    def test_write_rewrites_content(self) -> None:
        rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)")
        evt = pre_event("Write", {"file_path": "d.py", "content": 'os.system("ls")\n'})
        assert updated_input(dispatch(Event.PreToolUse, evt))["content"] == 'subprocess.run(["ls"], check=True)\n'

    def test_multiedit_rewrites_each_field(self) -> None:
        rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)")
        evt = pre_event(
            "MultiEdit",
            {
                "file_path": "d.py",
                "edits": [
                    {"old_string": "a", "new_string": 'os.system("a")\n'},
                    {"old_string": "b", "new_string": "x = 1\n"},
                ],
            },
        )
        out = updated_input(dispatch(Event.PreToolUse, evt))
        assert out["edits"][0]["new_string"] == 'subprocess.run(["a"], check=True)\n'
        assert out["edits"][1]["new_string"] == "x = 1\n"

    def test_notebook_edit_rewrites_new_source(self) -> None:
        # A .ipynb extension carries no cell language, so a notebook rewrite needs an explicit lang.
        rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)", lang="py")
        evt = pre_event("NotebookEdit", {"notebook_path": "n.ipynb", "new_source": 'os.system("ls")\n'})
        assert updated_input(dispatch(Event.PreToolUse, evt))["new_source"] == 'subprocess.run(["ls"], check=True)\n'

    def test_lang_override_for_unknown_extension(self) -> None:
        rewrite_code("console.log($A)", "logger.info($A)", lang="ts")
        out = updated_input(dispatch(Event.PreToolUse, edit("console.log(x)\n", file="app.weird")))
        assert out["new_string"] == "logger.info(x)\n"

    def test_unknown_extension_without_lang_passes_through(self) -> None:
        rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)")
        assert dispatch(Event.PreToolUse, edit('os.system("ls")\n', file="notes.txt")) is None

    def test_project_only_skips_external_file(self) -> None:
        rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)", project_only=True)
        evt = pre_event(
            "Edit",
            {"file_path": "/elsewhere/x.py", "old_string": "", "new_string": 'os.system("ls")\n'},
            project_root=Path("/repo"),
        )
        assert dispatch(Event.PreToolUse, evt) is None

    def test_project_only_false_rewrites_external_file(self) -> None:
        rewrite_code("os.system($CMD)", "subprocess.run([$CMD], check=True)", project_only=False)
        evt = pre_event(
            "Edit",
            {"file_path": "/elsewhere/x.py", "old_string": "", "new_string": 'os.system("ls")\n'},
            project_root=Path("/repo"),
        )
        assert updated_input(dispatch(Event.PreToolUse, evt))["new_string"] == 'subprocess.run(["ls"], check=True)\n'


class TestCommandLineStructural:
    def test_matches(self) -> None:
        assert CommandLine.parse("cat -n foo.txt").matches("cat $$$ARGS")
        assert not CommandLine.parse("ls -la").matches("cat $$$ARGS")

    @pytest.mark.parametrize(
        ("raw", "pattern", "replacement", "expected"),
        [
            pytest.param(
                "cat a.txt | grep x", "cat $$$A", "bat $$$A", "bat a.txt | grep x", id="pipeline_only_rewrites_match"
            ),
            pytest.param("cat -n foo.txt", "cat $F", "bat $F", "bat -n", id="single_metavar_drops_extra_args_footgun"),
            pytest.param("ls -la", "cat $$$A", "bat $$$A", "ls -la", id="no_match_unchanged"),
        ],
    )
    def test_rewrite(self, raw: str, pattern: str, replacement: str, expected: str) -> None:
        assert CommandLine.parse(raw).rewrite(pattern, replacement) == expected


class TestRewriteCommandDispatch:
    @pytest.mark.parametrize(
        ("pattern", "replacement", "note", "command", "expected"),
        [
            pytest.param(
                "cat $$$ARGS",
                "bat $$$ARGS",
                "use bat",
                "cat -n foo.txt",
                "bat -n foo.txt",
                id="metavar_routes_to_structural",
            ),
            pytest.param(
                r"^cat\s+(\S+)$", r"bat \1", None, "cat foo.txt", "bat foo.txt", id="regex_pattern_stays_resub"
            ),
            pytest.param(
                "cat $$$A",
                "bat $$$A && echo $HOME",
                None,
                "cat foo.txt",
                "bat foo.txt && echo $HOME",
                id="shell_var_in_replacement_stays_literal",
            ),
        ],
    )
    def test_command_rewrite(
        self, pattern: str, replacement: str, note: str | None, command: str, expected: str
    ) -> None:
        rewrite_command(pattern, replacement, note=note)
        out = updated_input(dispatch(Event.PreToolUse, bash(command)))
        assert out["command"] == expected

    def test_structural_no_match_passes_through(self) -> None:
        rewrite_command("cat $$$ARGS", "bat $$$ARGS")
        assert dispatch(Event.PreToolUse, bash("ls -la")) is None

    def test_callable_note_rejected_in_shorthand(self) -> None:
        with pytest.raises(TypeError, match="non-callable note"):
            rewrite_command("cat $F", "bat $F", note=lambda e: "x")
