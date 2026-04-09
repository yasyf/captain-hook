from __future__ import annotations

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

    def test_empty_string(self) -> None:
        cmd = Command.parse("")
        assert not bool(cmd)

    def test_vcs_command(self) -> None:
        cmd = Command.parse('jj commit -m "some message"')
        assert cmd.executable == "jj"
        assert cmd.args[0] == "commit"

    def test_program_uv_run(self) -> None:
        assert Command.parse("uv run mtest run tests/").program == "mtest"

    def test_program_uv_run_pytest(self) -> None:
        assert Command.parse("uv run pytest tests/").program == "pytest"

    def test_program_python_m(self) -> None:
        assert Command.parse("python -m module arg").program == "module"

    def test_program_python3_m(self) -> None:
        assert Command.parse("python3 -m module arg").program == "module"

    def test_program_plain_executable(self) -> None:
        assert Command.parse("jj commit").program == "jj"

    def test_argv_includes_executable_and_args(self) -> None:
        cmd = Command.parse("cat file.py")
        assert cmd.argv == ("cat", "file.py")

    def test_argv_empty_for_empty_command(self) -> None:
        cmd = Command.parse("")
        assert cmd.argv == ()

    def test_matches_regex(self) -> None:
        cmd = Command.parse("jj commit -m x")
        assert cmd.matches(r"jj\s+(commit|split)")

    def test_no_match(self) -> None:
        cmd = Command.parse("jj log")
        assert not cmd.matches(r"jj\s+(commit|split)")

    def test_has_arg(self) -> None:
        cmd = Command.parse("uv run mtest run tests/ --last-failed")
        assert cmd.has_arg(r"--last-failed")

    def test_has_arg_k(self) -> None:
        cmd = Command.parse("uv run mtest run tests/ -k test_name")
        assert cmd.has_arg(r"^-k$")

    def test_no_arg(self) -> None:
        cmd = Command.parse("uv run mtest run tests/")
        assert not cmd.has_arg(r"--last-failed")

    def test_contains(self) -> None:
        assert ".py" in Command.parse("uv run mtest run tests/test_foo.py")

    def test_not_contains(self) -> None:
        assert ".py" not in Command.parse("jj commit -m msg")

    def test_str_strips_env(self) -> None:
        cmd = Command.parse("ENV=val uv run mtest")
        assert str(cmd) == "uv run mtest"

    def test_str_simple(self) -> None:
        assert str(Command.parse("jj commit")) == "jj commit"

    def test_empty_is_falsy(self) -> None:
        assert not Command.parse("")

    def test_non_empty_is_truthy(self) -> None:
        assert Command.parse("cat")

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
    def test_simple(self) -> None:
        cl = CommandLine.parse("jj commit")
        assert len(cl) == 1
        assert cl.primary.executable == "jj"

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

    def test_semicolon_chain(self) -> None:
        cl = CommandLine.parse("cmd1; cmd2")
        assert len(cl) == 2
        assert cl.parts[0][1] == ";"

    def test_or_chain(self) -> None:
        cl = CommandLine.parse("cmd1 || cmd2")
        assert len(cl) == 2
        assert cl.parts[0][1] == "||"

    def test_mixed_operators(self) -> None:
        cl = CommandLine.parse("cmd1; cmd2 && cmd3")
        assert len(cl) == 3
        assert cl.primary.executable == "cmd3"

    def test_primary_is_last(self) -> None:
        cl = CommandLine.parse("cd /dir && ./setup.sh")
        assert cl.primary.executable == "./setup.sh"

    def test_primary_simple(self) -> None:
        cl = CommandLine.parse("jj commit")
        assert cl.primary.executable == "jj"

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

    def test_contains_raw(self) -> None:
        cl = CommandLine.parse('eval "$(direnv)" && uv run')
        assert "direnv" in cl

    def test_not_contains(self) -> None:
        assert "direnv" not in CommandLine.parse("jj commit")

    def test_truthy(self) -> None:
        assert bool(CommandLine.parse("cmd"))

    def test_pipe_heredoc_not_treated_as_command(self) -> None:
        cl = CommandLine.parse("cat <<EOF\ngit push --force\nEOF")
        assert cl.primary.executable == "cat"
        assert not any(cmd.executable == "git" for cmd in cl.commands)

    def test_subshell_parsed(self) -> None:
        cl = CommandLine.parse('eval "$(direnv export bash)"')
        assert len(cl) >= 1


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
