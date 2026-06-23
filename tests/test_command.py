from __future__ import annotations

import pytest

from captain_hook.command import Command, CommandLine


class TestCommand:
    def test_simple_command(self) -> None:
        cmd = Command.parse("cat file.py")
        assert cmd.executable == "cat"
        assert cmd.args == ("file.py",)

    def test_env_vars(self) -> None:
        cmd = Command.parse("ENV=val uv run pytest")
        assert cmd.executable == "uv"
        assert cmd.env_dict == {"ENV": "val"}

    def test_multiple_env_vars(self) -> None:
        cmd = Command.parse("ENV_VAR=val OTHER=x uv run pytest tests/")
        assert cmd.executable == "uv"
        assert cmd.env == (("ENV_VAR", "val"), ("OTHER", "x"))
        assert cmd.env_dict == {"ENV_VAR": "val", "OTHER": "x"}

    def test_vcs_command(self) -> None:
        cmd = Command.parse('jj commit -m "some message"')
        assert cmd.executable == "jj"
        assert cmd.args[0] == "commit"

    @pytest.mark.parametrize(
        ("raw", "program"),
        [
            pytest.param("uv run mtest run tests/", "mtest", id="uv_run"),
            pytest.param("uv run pytest tests/", "pytest", id="uv_run_pytest"),
            pytest.param("python -m module arg", "module", id="python_m"),
            pytest.param("python3 -m module arg", "module", id="python3_m"),
            pytest.param("jj commit", "jj", id="plain_executable"),
        ],
    )
    def test_program(self, raw: str, program: str) -> None:
        assert Command.parse(raw).program == program

    @pytest.mark.parametrize(
        ("raw", "argv"),
        [
            pytest.param("cat file.py", ("cat", "file.py"), id="includes_executable_and_args"),
            pytest.param("", (), id="empty_command"),
        ],
    )
    def test_argv(self, raw: str, argv: tuple[str, ...]) -> None:
        assert Command.parse(raw).argv == argv

    @pytest.mark.parametrize(
        ("raw", "pattern", "expected"),
        [
            pytest.param("jj commit -m x", r"jj\s+(commit|split)", True, id="matches_regex"),
            pytest.param("jj log", r"jj\s+(commit|split)", False, id="no_match"),
        ],
    )
    def test_matches(self, raw: str, pattern: str, expected: bool) -> None:
        assert bool(Command.parse(raw).matches(pattern)) is expected

    @pytest.mark.parametrize(
        ("raw", "pattern", "expected"),
        [
            pytest.param("uv run mtest run tests/ --last-failed", r"--last-failed", True, id="has_arg"),
            pytest.param("uv run mtest run tests/ -k test_name", r"^-k$", True, id="has_arg_k"),
            pytest.param("uv run mtest run tests/", r"--last-failed", False, id="no_arg"),
        ],
    )
    def test_has_arg(self, raw: str, pattern: str, expected: bool) -> None:
        assert bool(Command.parse(raw).has_arg(pattern)) is expected

    @pytest.mark.parametrize(
        ("raw", "needle", "expected"),
        [
            pytest.param("uv run mtest run tests/test_foo.py", ".py", True, id="contains"),
            pytest.param("jj commit -m msg", ".py", False, id="not_contains"),
        ],
    )
    def test_contains(self, raw: str, needle: str, expected: bool) -> None:
        assert (needle in Command.parse(raw)) is expected

    @pytest.mark.parametrize(
        ("raw", "rendered"),
        [
            pytest.param("ENV=val uv run mtest", "uv run mtest", id="strips_env"),
            pytest.param("jj commit", "jj commit", id="simple"),
        ],
    )
    def test_str(self, raw: str, rendered: str) -> None:
        assert str(Command.parse(raw)) == rendered

    @pytest.mark.parametrize(
        ("raw", "truthy"),
        [
            pytest.param("", False, id="empty_string"),
            pytest.param("", False, id="empty_is_falsy"),
            pytest.param("cat", True, id="non_empty_is_truthy"),
        ],
    )
    def test_truthiness(self, raw: str, truthy: bool) -> None:
        assert bool(Command.parse(raw)) is truthy

    def test_append_redirect(self) -> None:
        cmd = Command.parse("echo hello >> out.txt")
        assert len(cmd.redirects) == 1
        assert cmd.redirects[0].op == ">>"
        assert cmd.redirects[0].target == "out.txt"
        assert cmd.redirects[0].fd is None

    def test_fd_redirect(self) -> None:
        cmd = Command.parse("cmd 2>&1")
        assert len(cmd.redirects) == 1
        assert cmd.redirects[0].op == ">&"
        assert cmd.redirects[0].fd == 2

    def test_multiple_redirects(self) -> None:
        cmd = Command.parse("echo hello >> out.txt 2>&1")
        assert len(cmd.redirects) == 2


class TestCommandLine:
    @pytest.mark.parametrize(
        ("raw", "length", "primary_executable"),
        [
            pytest.param("jj commit", 1, "jj", id="simple"),
            pytest.param("cmd1; cmd2 && cmd3", 3, "cmd3", id="mixed_operators"),
        ],
    )
    def test_length_and_primary(self, raw: str, length: int, primary_executable: str) -> None:
        cl = CommandLine.parse(raw)
        assert len(cl) == length
        assert cl.primary.executable == primary_executable

    @pytest.mark.parametrize(
        ("raw", "op"),
        [
            pytest.param("cmd1; cmd2", ";", id="semicolon_chain"),
            pytest.param("cmd1 || cmd2", "||", id="or_chain"),
        ],
    )
    def test_two_part_chain(self, raw: str, op: str) -> None:
        cl = CommandLine.parse(raw)
        assert len(cl) == 2
        assert cl.parts[0][1] == op

    @pytest.mark.parametrize(
        ("raw", "executable"),
        [
            pytest.param("jj commit", "jj", id="primary_simple"),
            pytest.param("cd /dir && ./setup.sh", "./setup.sh", id="primary_is_last"),
        ],
    )
    def test_primary_executable(self, raw: str, executable: str) -> None:
        assert CommandLine.parse(raw).primary.executable == executable

    def test_and_chain(self) -> None:
        cl = CommandLine.parse('eval "$(direnv export bash)" && uv run mtest run tests/')
        assert len(cl) == 2
        assert cl.parts[0][1] == "&&"
        assert cl.primary.executable == "uv"
        assert cl.primary.program == "mtest"

    def test_pipe_chain(self) -> None:
        cl = CommandLine.parse("cat file.py | grep pattern")
        assert len(cl) == 2
        assert cl.parts[0][1] == "|"
        assert cl.commands[0].executable == "cat"
        assert cl.commands[1].executable == "grep"

    def test_commands_tuple(self) -> None:
        cl = CommandLine.parse("a && b")
        assert len(cl.commands) == 2
        assert cl.commands[0].executable == "a"
        assert cl.commands[1].executable == "b"

    def test_iter(self) -> None:
        cl = CommandLine.parse("cmd1 && cmd2 && cmd3")
        execs = [cmd.executable for cmd in cl]
        assert execs == ["cmd1", "cmd2", "cmd3"]

    def test_len(self) -> None:
        assert len(CommandLine.parse("a && b && c")) == 3

    def test_str_returns_raw(self) -> None:
        raw = "a && b"
        assert str(CommandLine.parse(raw)) == raw

    @pytest.mark.parametrize(
        ("raw", "needle", "expected"),
        [
            pytest.param('eval "$(direnv)" && uv run', "direnv", True, id="contains_raw"),
            pytest.param("jj commit", "direnv", False, id="not_contains"),
        ],
    )
    def test_contains(self, raw: str, needle: str, expected: bool) -> None:
        assert (needle in CommandLine.parse(raw)) is expected

    def test_truthy(self) -> None:
        assert bool(CommandLine.parse("cmd"))

    def test_pipe_heredoc_not_treated_as_command(self) -> None:
        cl = CommandLine.parse("cat <<EOF\ngit push --force\nEOF")
        assert cl.primary.executable == "cat"
        assert not any(cmd.executable == "git" for cmd in cl.commands)

    def test_subshell_parsed(self) -> None:
        cl = CommandLine.parse('eval "$(direnv export bash)"')
        assert len(cl) >= 1


class TestCommandLineCapture:
    @pytest.mark.parametrize(
        ("raw", "pattern", "captured"),
        [
            pytest.param("sed -n 10,40p f.go", "sed -n $R $F", {"R": "10,40p", "F": "f.go"}, id="named_singles"),
            pytest.param("cat a.txt b.txt", "cat $$$FILES", {"FILES": "a.txt b.txt"}, id="triple_metavar_span"),
            pytest.param("ls -la", "sed -n $R $F", None, id="no_match_is_none"),
        ],
    )
    def test_capture(self, raw: str, pattern: str, captured: dict[str, str] | None) -> None:
        assert CommandLine.parse(raw).capture(pattern) == captured


class TestEdgeCases:
    def test_malformed_quote(self) -> None:
        cmd = Command.parse('echo "unterminated')
        assert cmd.executable == "echo"

    def test_empty_commandline(self) -> None:
        cl = CommandLine.parse("")
        assert isinstance(cl, CommandLine)

    def test_colons_preserved(self) -> None:
        cmd = Command.parse("uv run mtest run tests/test_foo.py::TestClass::test_method")
        assert any("::TestClass::test_method" in a for a in cmd.args)

    def test_complex_direnv(self) -> None:
        cl = CommandLine.parse(
            'eval "$(direnv export bash)" && ENV=prod uv run mtest run tests/test_foo.py -k test_name 2>&1 | head -50'
        )
        assert len(cl) >= 3
        assert cl.primary.executable == "head"

        mtest_cmd = next(cmd for cmd in cl if cmd.program == "mtest")
        assert mtest_cmd.env == (("ENV", "prod"),)
        assert mtest_cmd.has_arg(r"^-k$")
        assert any(r.op == ">&" and r.fd == 2 for r in mtest_cmd.redirects)
