from __future__ import annotations

import os
import subprocess
from typing import Any

import pytest

from captain_hook.util import proc, reqenv
from captain_hook.util.proc import _SKIP_CACHE, MAX_WALK, _cold_skip_permissions, claude_skip_permissions, parent_entry

HOOK_CMD = "/private/tmp/claude-501/-Users-yasyf-Code-captain-hook/wt/.venv/bin/capt-hook run PermissionRequest"
SHELL_CMD = "/bin/zsh -c eval capt-hook && pwd -P >| /tmp/claude-d9da-cwd"


@pytest.fixture(autouse=True)
def clear_walk_cache():
    _cold_skip_permissions.cache_clear()
    _SKIP_CACHE.cache_clear()
    yield
    _cold_skip_permissions.cache_clear()
    _SKIP_CACHE.cache_clear()


def bound(client_ppid: int, session_id: str) -> reqenv.RequestOverrides:
    return reqenv.RequestOverrides(
        env={}, cwd="/w", client_pid=client_ppid + 1, client_ppid=client_ppid, session_id=session_id
    )


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

    def test_caches_per_session_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        walked: list[int] = []

        def fake(pid: int) -> tuple[int, str] | None:
            walked.append(pid)
            return {50: (1, "claude --dangerously-skip-permissions")}.get(pid)

        monkeypatch.setattr(proc, "parent_entry", fake)
        with reqenv.use_request(bound(client_ppid=50, session_id="s1")):
            assert claude_skip_permissions() is True
            assert claude_skip_permissions() is True
        assert walked == [50]

    def test_distinct_sessions_resolve_independently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        table = {50: (1, "claude --dangerously-skip-permissions"), 60: (1, "claude --permission-mode plan")}
        monkeypatch.setattr(proc, "parent_entry", lambda pid: table.get(pid))
        with reqenv.use_request(bound(client_ppid=50, session_id="s1")):
            assert claude_skip_permissions() is True
        with reqenv.use_request(bound(client_ppid=60, session_id="s2")):
            assert claude_skip_permissions() is False
