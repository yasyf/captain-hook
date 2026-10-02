from __future__ import annotations

import os
import subprocess
from datetime import datetime
from typing import Any

import pytest

from captain_hook.util import proc, reqenv
from captain_hook.util.proc import (
    MAX_WALK,
    PS_TABLE_ARGV,
    PS_TABLE_ENV,
    ProcessRow,
    ProcessTable,
    _cold_claude_argv,
    claude_disallowed_tools,
    claude_skip_permissions,
    disallowed_tools,
    parent_entry,
    process_table,
)

HOOK_CMD = "/private/tmp/claude-501/-Users-yasyf-Code-captain-hook/wt/.venv/bin/capt-hook run PermissionRequest"
SHELL_CMD = "/bin/zsh -c eval capt-hook && pwd -P >| /tmp/claude-d9da-cwd"
PS_TABLE = (
    "    1     0     1    0 Wed Sep 30 05:54:44 2026 /sbin/launchd\n"
    "  310     1   310   -2 Wed Sep 30 05:54:50 2026 /usr/sbin/mDNSResponder\n"
    " 1445     1  1445  501 Wed Sep  5 05:58:20 2026 /Applications/Orca.app/Contents/MacOS/Orca\n"
    " 1743  1445  1743  501 Wed Sep 30 05:58:30 2026 /Applications/Orca.app/Contents/Frameworks/Orca Helper.app"
    "/Contents/MacOS/Orca Helper /Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/main/daemon-entry.js"
    " --socket /Users/y/Library/Application Support/orca/daemon/daemon-v36.sock\n"
    "14575  1743 14575  501 Wed Sep 30 06:01:05 2026 claude --dangerously-skip-permissions\n"
)


def row(pid: int, ppid: int, command: str, *, uid: int = 501, started: str = "2026-09-30T06:00:00") -> ProcessRow:
    return ProcessRow(pid, ppid, pid, uid, datetime.fromisoformat(started), command)


def table(*rows: ProcessRow) -> ProcessTable:
    return ProcessTable({entry.pid: entry for entry in rows})


class TestProcessTable:
    def test_parses_a_full_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(args: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert args == PS_TABLE_ARGV
            assert kwargs["env"] == os.environ | PS_TABLE_ENV
            assert kwargs["timeout"] == 2
            return subprocess.CompletedProcess(args, 0, stdout=PS_TABLE, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        snapshot = process_table()
        assert snapshot is not None
        assert set(snapshot.rows) == {1, 310, 1445, 1743, 14575}
        assert snapshot.rows[310].uid == -2
        assert snapshot.rows[1445].started == datetime(2026, 9, 5, 5, 58, 20)
        assert snapshot.rows[1743].argv0 == "Orca"
        assert snapshot.rows[1743].command.endswith("daemon-v36.sock")
        assert snapshot.rows[14575] == ProcessRow(
            14575, 1743, 14575, 501, datetime(2026, 9, 30, 6, 1, 5), "claude --dangerously-skip-permissions"
        )

    def test_timeout_is_the_callers_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[float] = []

        def fake_run(args: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen.append(kwargs["timeout"])
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with reqenv.use_request(bound(client_ppid=50, session_id="s")), reqenv.deadline_in(60):
            assert process_table(timeout=0.75) == ProcessTable({})
        assert seen == [0.75]

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(OSError("no ps"), id="os_error"),
            pytest.param(subprocess.TimeoutExpired("ps", 2), id="timeout"),
        ],
    )
    def test_ps_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise exc

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert process_table() is None

    @pytest.mark.parametrize(
        ("returncode", "stdout"),
        [
            pytest.param(1, PS_TABLE, id="nonzero_exit"),
            pytest.param(0, PS_TABLE + "garbage row\n", id="malformed_row"),
            pytest.param(0, "  12  1  12  501 Wed Sep 30 2026 sleep\n", id="truncated_lstart"),
        ],
    )
    def test_unparseable_output_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
    ) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda args, **kw: subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=""),
        )
        assert process_table() is None

    def test_ancestors_are_strict_and_root_most_last(self) -> None:
        snapshot = table(row(1, 0, "/sbin/launchd"), row(10, 1, "login"), row(20, 10, "fish"), row(30, 20, "claude"))
        assert [entry.pid for entry in snapshot.ancestors(30)] == [20, 10, 1]
        assert snapshot.ancestors(1) == ()
        assert snapshot.ancestors(999) == ()

    def test_ancestors_terminate_on_a_cycle(self) -> None:
        snapshot = table(row(5, 6, "a"), row(6, 7, "b"), row(7, 5, "c"), row(8, 8, "self"))
        assert [entry.pid for entry in snapshot.ancestors(5)] == [6, 7]
        assert snapshot.ancestors(8) == ()

    def test_descendants_cover_every_generation_once(self) -> None:
        snapshot = table(
            row(1, 0, "/sbin/launchd"),
            row(10, 1, "login"),
            row(20, 10, "fish"),
            row(30, 20, "claude"),
            row(31, 20, "sleep 5"),
            row(40, 1, "node server.js"),
            row(50, 50, "self"),
        )
        assert {entry.pid for entry in snapshot.descendants(10)} == {20, 30, 31}
        assert {entry.pid for entry in snapshot.descendants(1)} == {10, 20, 30, 31, 40}
        assert snapshot.descendants(30) == ()
        assert snapshot.descendants(50) == ()
        assert snapshot.descendants(999) == ()

    def test_nearest_checks_the_pid_itself_then_ancestors(self) -> None:
        snapshot = table(row(1, 0, "launchd"), row(10, 1, "claude -p"), row(11, 10, "zsh -c hook"))
        assert (found := snapshot.nearest(11, lambda entry: entry.argv0 == "claude")) is not None
        assert found.pid == 10
        assert (own := snapshot.nearest(10, lambda entry: entry.argv0 == "claude")) is not None
        assert own.pid == 10
        assert snapshot.nearest(11, lambda entry: entry.argv0 == "codex") is None
        assert snapshot.nearest(999, lambda entry: True) is None


@pytest.fixture(autouse=True)
def clear_walk_cache():
    _cold_claude_argv.cache_clear()
    yield
    _cold_claude_argv.cache_clear()


def bound(client_ppid: int, session_id: str) -> reqenv.RequestOverrides:
    return reqenv.RequestOverrides(env={}, cwd="/w", client_ppid=client_ppid, session_id=session_id)


def install_chain(monkeypatch: pytest.MonkeyPatch, commands: list[str]) -> None:
    """Fake the process tree: entry i is pid ``base+i`` running ``commands[i]``; the last parent is pid 1."""
    base = os.getpid()
    table = {base + i: (1 if i == len(commands) - 1 else base + i + 1, command) for i, command in enumerate(commands)}
    monkeypatch.setattr(proc, "parent_entry", lambda pid: table.get(pid))


class TestParentEntry:
    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [
            pytest.param("  123 claude --permission-mode plan\n", (123, "claude --permission-mode plan"), id="padded"),
            pytest.param("1 /sbin/launchd\n", (1, "/sbin/launchd"), id="pid_one"),
            pytest.param("  42\n", (42, ""), id="no_command"),
            pytest.param("", None, id="empty_output"),
        ],
    )
    def test_parses_ps_output(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str, expected: tuple[int, str] | None
    ) -> None:
        def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert args == ["ps", "-o", "ppid=,command=", "-p", "777"]
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert parent_entry(777) == expected

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(OSError("no ps"), id="os_error"),
            pytest.param(subprocess.CalledProcessError(1, "ps"), id="nonzero_exit"),
            pytest.param(subprocess.TimeoutExpired("ps", 5), id="timeout"),
        ],
    )
    def test_ps_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise exc

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert parent_entry(777) is None


class TestClaudeSkipPermissions:
    @pytest.mark.parametrize(
        ("claude_cmd", "expected"),
        [
            pytest.param("/Users/y/.local/bin/claude --dangerously-skip-permissions", True, id="double_dash"),
            pytest.param("claude --allow-dangerously-skip-permissions", True, id="allow_spelling"),
            pytest.param(
                "/Users/y/.local/bin/claude --allow-dangerously-skip-permissions --permission-mode plan",
                True,
                id="allow_plus_plan",
            ),
            pytest.param("claude --permission-mode plan", False, id="permission_mode_no_flag"),
            pytest.param("claude -p do-the-thing", False, id="no_flag"),
            pytest.param("claude -dangerously-skip-permissions", False, id="single_dash_not_a_flag"),
            pytest.param("claude cp /docs/--dangerously-skip-permissions/notes.txt /tmp/x", False, id="flag_as_path"),
        ],
    )
    def test_flag_token_on_nearest_claude(
        self, monkeypatch: pytest.MonkeyPatch, claude_cmd: str, expected: bool
    ) -> None:
        install_chain(monkeypatch, [HOOK_CMD, SHELL_CMD, claude_cmd])
        assert claude_skip_permissions() is expected

    def test_own_scratch_paths_do_not_shadow_real_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_chain(
            monkeypatch,
            [
                HOOK_CMD,
                SHELL_CMD,
                "/Users/yasyf/.claude/plugins/cache/cc-review/bin/cc-review mcp-channel",
                "/Users/yasyf/.local/bin/claude --allow-dangerously-skip-permissions --permission-mode plan",
            ],
        )
        assert claude_skip_permissions() is True

    @pytest.mark.parametrize(
        "wrapper_cmd",
        [
            pytest.param("/bin/zsh -c claude-helper --dangerously-skip-permissions", id="flag_token_in_wrapper"),
            pytest.param("/bin/bash /opt/--dangerously-skip-permissions/run.sh", id="flag_inside_path"),
        ],
    )
    def test_flag_in_non_claude_ancestor_argv_is_not_consent(
        self, monkeypatch: pytest.MonkeyPatch, wrapper_cmd: str
    ) -> None:
        install_chain(monkeypatch, [HOOK_CMD, wrapper_cmd, "claude --permission-mode plan"])
        assert claude_skip_permissions() is False

    def test_flag_in_argv_without_any_claude_ancestor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_chain(monkeypatch, [HOOK_CMD, "/bin/bash /opt/--dangerously-skip-permissions/run.sh"])
        assert claude_skip_permissions() is False

    @pytest.mark.parametrize(
        "impostor_cmd",
        [
            pytest.param("/usr/bin/claude-foo --dangerously-skip-permissions", id="claude_prefixed_binary"),
            pytest.param("/opt/tools/claude.py --dangerously-skip-permissions", id="claude_dot_py"),
        ],
    )
    def test_claude_like_basenames_are_not_claude(self, monkeypatch: pytest.MonkeyPatch, impostor_cmd: str) -> None:
        install_chain(monkeypatch, [HOOK_CMD, impostor_cmd])
        assert claude_skip_permissions() is False

    @pytest.mark.parametrize(
        ("runtime_cmd", "expected"),
        [
            pytest.param(
                "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js --dangerously-skip-permissions",
                True,
                id="node_claude_code_cli",
            ),
            pytest.param(
                "bun /Users/y/src/claude-code/cli.js --allow-dangerously-skip-permissions",
                True,
                id="bun_source_install",
            ),
            pytest.param("node /srv/app/cli.js --dangerously-skip-permissions", False, id="unrelated_cli_js"),
            pytest.param(
                "node /srv/claude-code/server.js --dangerously-skip-permissions", False, id="claude_dir_wrong_script"
            ),
        ],
    )
    def test_js_runtime_claude_cli(self, monkeypatch: pytest.MonkeyPatch, runtime_cmd: str, expected: bool) -> None:
        install_chain(monkeypatch, [HOOK_CMD, SHELL_CMD, runtime_cmd])
        assert claude_skip_permissions() is expected

    def test_nearest_claude_without_flag_shadows_flagged_outer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_chain(
            monkeypatch,
            [
                HOOK_CMD,
                SHELL_CMD,
                "/Users/y/.local/bin/claude -p inner",
                "/bin/sh -c bash-tool",
                "claude --dangerously-skip-permissions",
            ],
        )
        assert claude_skip_permissions() is False

    def test_empty_command_entry_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_chain(monkeypatch, [HOOK_CMD, "", "claude --dangerously-skip-permissions"])
        assert claude_skip_permissions() is True

    def test_walk_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_chain(monkeypatch, ["/bin/sh -c wrapper"] * (MAX_WALK + 5) + ["claude --dangerously-skip-permissions"])
        assert claude_skip_permissions() is False

    def test_stops_at_pid_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        base = os.getpid()
        table = {
            base: (1, HOOK_CMD),
            1: (0, "claude --dangerously-skip-permissions"),
        }
        monkeypatch.setattr(proc, "parent_entry", lambda pid: table.get(pid))
        assert claude_skip_permissions() is False

    def test_ps_failure_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(proc, "parent_entry", lambda pid: None)
        assert claude_skip_permissions() is False


class TestRequestBoundSkipPermissions:
    def test_walks_from_client_ppid_not_self(self, monkeypatch: pytest.MonkeyPatch) -> None:
        table = {50: (1, "claude --dangerously-skip-permissions")}
        monkeypatch.setattr(proc, "parent_entry", lambda pid: table.get(pid))
        with reqenv.use_request(bound(client_ppid=50, session_id="s1")):
            assert claude_skip_permissions() is True

    def test_same_session_flag_gain_reflected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        table = {50: (1, "claude --permission-mode plan")}
        monkeypatch.setattr(proc, "parent_entry", lambda pid: table.get(pid))
        with reqenv.use_request(bound(client_ppid=50, session_id="s1")):
            assert claude_skip_permissions() is False
        table[50] = (1, "claude --dangerously-skip-permissions")
        with reqenv.use_request(bound(client_ppid=50, session_id="s1")):
            assert claude_skip_permissions() is True

    def test_same_session_flag_drop_reflected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        table = {50: (1, "claude --dangerously-skip-permissions")}
        monkeypatch.setattr(proc, "parent_entry", lambda pid: table.get(pid))
        with reqenv.use_request(bound(client_ppid=50, session_id="s1")):
            assert claude_skip_permissions() is True
        table[50] = (1, "claude --permission-mode plan")
        with reqenv.use_request(bound(client_ppid=50, session_id="s1")):
            assert claude_skip_permissions() is False

    def test_distinct_sessions_resolve_independently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        table = {50: (1, "claude --dangerously-skip-permissions"), 60: (1, "claude --permission-mode plan")}
        monkeypatch.setattr(proc, "parent_entry", lambda pid: table.get(pid))
        with reqenv.use_request(bound(client_ppid=50, session_id="s1")):
            assert claude_skip_permissions() is True
        with reqenv.use_request(bound(client_ppid=60, session_id="s2")):
            assert claude_skip_permissions() is False

    def test_bypass_ppid_does_not_poison_a_later_same_session_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # S3: a bypass-signalling ppid must not flip a later same-session request whose ppid does not.
        table = {50: (1, "claude --dangerously-skip-permissions"), 60: (1, "claude --permission-mode plan")}
        monkeypatch.setattr(proc, "parent_entry", lambda pid: table.get(pid))
        with reqenv.use_request(bound(client_ppid=50, session_id="shared")):
            assert claude_skip_permissions() is True
        with reqenv.use_request(bound(client_ppid=60, session_id="shared")):
            assert claude_skip_permissions() is False


ORCA_WORKER_CMD = (
    "claude --allow-dangerously-skip-permissions --permission-mode bypassPermissions "
    "--disallowedTools AskUserQuestion,EnterPlanMode,ExitPlanMode --model claude-opus-5-5 --effort xhigh"
)


class TestDisallowedTools:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param(ORCA_WORKER_CMD, {"AskUserQuestion", "EnterPlanMode", "ExitPlanMode"}, id="orca_worker"),
            pytest.param("claude --disallowed-tools Edit Write -p go", {"Edit", "Write"}, id="kebab_variadic"),
            pytest.param("claude --disallowedTools=Edit,Write --model x", {"Edit", "Write"}, id="inline_value"),
            pytest.param("claude --disallowedTools Edit --disallowedTools Bash", {"Edit", "Bash"}, id="repeated"),
            pytest.param("claude --allowedTools ExitPlanMode --model x", set(), id="allowed_tools_flag"),
            pytest.param("claude -p ExitPlanMode", set(), id="bare_token"),
            pytest.param("claude -- --disallowedTools=ExitPlanMode", set(), id="after_end_of_options"),
        ],
    )
    def test_parses_flag_values(self, command: str, expected: set[str]) -> None:
        assert disallowed_tools(command.split()) == expected

    def test_reads_nearest_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_chain(monkeypatch, [HOOK_CMD, SHELL_CMD, ORCA_WORKER_CMD])
        assert "ExitPlanMode" in claude_disallowed_tools()
        assert claude_skip_permissions() is True

    def test_flag_in_non_claude_ancestor_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_chain(monkeypatch, [HOOK_CMD, "/bin/zsh -c run --disallowedTools ExitPlanMode", "claude -p go"])
        assert claude_disallowed_tools() == frozenset()

    def test_request_bound_walks_from_client_ppid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        table = {50: (1, ORCA_WORKER_CMD)}
        monkeypatch.setattr(proc, "parent_entry", lambda pid: table.get(pid))
        with reqenv.use_request(bound(client_ppid=50, session_id="s1")):
            assert "ExitPlanMode" in claude_disallowed_tools()
