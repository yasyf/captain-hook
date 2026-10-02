from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from captain_hook.procwatch.identity import ProcessIdentity, start_unix, verify
from captain_hook.procwatch.ownership import Owned, prove
from captain_hook.procwatch.signal import Outcome, terminate
from captain_hook.util import reqenv
from captain_hook.util.proc import (
    LSOF_CWD_ARGV,
    PS_ROW_ARGV,
    PS_TABLE_ENV,
    ProcessRow,
    ProcessTable,
    Unreadable,
    children,
    process_cwd,
    usage_row,
)
from tests.test_pack_sessions import MAC, NESTED, OWN_SLEEP, REUSED_PID
from tests.test_proc import row, table

CLAUDE = MAC.rows[14575]
ROW_LINE = "31337 27200 31337   501 Wed Sep 30 06:30:00 2026 sleep 60\n"
DEEP = 100


@pytest.fixture(autouse=True)
def fixture_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 501)


def unix(started: str) -> int:
    return int(datetime.fromisoformat(started).replace(tzinfo=UTC).timestamp())


def identity_of(entry: ProcessRow, *, cwd: str | None = "/w") -> ProcessIdentity:
    return ProcessIdentity(entry.pid, entry.ppid, entry.pgid, start_unix(entry), tuple(entry.command.split()), cwd)


def payload(**overrides: Any) -> dict[str, Any]:
    return {
        "pid": 31337,
        "ppid": 27200,
        "pgid": 31337,
        "start_unix": unix("2026-09-30T06:30:00"),
        "argv": ["sleep", "60"],
        "cwd": "/w",
        "comm": "sleep",
        "runtime_s": 130,
    } | overrides


def with_rows(base: ProcessTable, *extra: ProcessRow) -> ProcessTable:
    return table(*base.rows.values(), *extra)


def chain(root: ProcessRow, depth: int, *, leaf: str = "/bin/zsh -c wrapper") -> tuple[ProcessRow, ...]:
    links = [root]
    for offset in range(1, depth + 1):
        links.append(row(root.pid + offset, links[-1].pid, "/bin/zsh -c wrapper", started="2026-09-30T06:31:01"))
    return (*links[1:-1], row(links[-1].pid, links[-2].pid, leaf, started="2026-09-30T06:31:01"))


def proved(snapshot: ProcessTable, identity: ProcessIdentity, **anchor: int) -> Owned | Unreadable:
    return prove(
        snapshot,
        identity,
        **({"claude_pid": CLAUDE.pid, "claude_start_unix": start_unix(CLAUDE)} | anchor),
    )


def refused(verdict: Owned | Unreadable) -> str:
    assert isinstance(verdict, Unreadable)
    return verdict.reason


class TestProcessIdentity:
    def test_from_payload_round_trip(self) -> None:
        identity = ProcessIdentity.from_payload(payload())
        assert identity == ProcessIdentity(31337, 27200, 31337, unix("2026-09-30T06:30:00"), ("sleep", "60"), "/w")
        assert identity.key == f"31337:{unix('2026-09-30T06:30:00')}"
        assert identity.command == "sleep 60"

    @pytest.mark.parametrize(
        "process",
        [
            pytest.param({k: v for k, v in payload().items() if k != "cwd"}, id="absent"),
            pytest.param(payload(cwd=""), id="empty"),
            pytest.param(payload(cwd=None), id="null"),
        ],
    )
    def test_cwd_is_optional(self, process: dict[str, Any]) -> None:
        assert ProcessIdentity.from_payload(process).cwd is None

    @pytest.mark.parametrize(
        "process",
        [
            pytest.param({k: v for k, v in payload().items() if k != "argv"}, id="missing_argv"),
            pytest.param(payload(argv=["sleep", 60]), id="non_string_arg"),
            pytest.param(payload(pid="31337"), id="string_pid"),
            pytest.param(payload(cwd=7), id="non_string_cwd"),
        ],
    )
    def test_rejects_malformed_payloads(self, process: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            ProcessIdentity.from_payload(process)


class Readers:
    def __init__(self, entry: ProcessRow | None, cwd: str | None, *, verdict: Unreadable | None = None) -> None:
        self.entry = entry
        self.cwd = cwd
        self.verdict = verdict
        self.calls: list[Any] = []

    def usage_row(self, pid: int) -> ProcessRow | None:
        self.calls.append(("row", pid))
        return self.entry

    def process_cwd(self, pid: int) -> str | None:
        self.calls.append(("cwd", pid))
        return self.cwd

    def recheck(self) -> Unreadable | None:
        self.calls.append(("recheck",))
        return self.verdict

    def kill(self, pid: int, sig: int) -> None:
        self.calls.append(("kill", pid, sig))

    def terminate(self, identity: ProcessIdentity, sig: int) -> Outcome:
        return terminate(
            identity,
            sig,
            recheck=self.recheck,
            kill=self.kill,
            usage_row=self.usage_row,
            process_cwd=self.process_cwd,
        )


class TestVerify:
    def test_reads_cwd_before_the_row(self) -> None:
        readers = Readers(OWN_SLEEP, "/w")
        assert verify(identity_of(OWN_SLEEP), usage_row=readers.usage_row, process_cwd=readers.process_cwd) == OWN_SLEEP
        assert readers.calls == [("cwd", 31337), ("row", 31337)]

    def test_refuses_without_a_recorded_cwd(self) -> None:
        readers = Readers(OWN_SLEEP, "/w")
        identity = identity_of(OWN_SLEEP, cwd=None)
        verdict = verify(identity, usage_row=readers.usage_row, process_cwd=readers.process_cwd)
        assert verdict == Unreadable("pid 31337: no recorded cwd to verify.")
        assert readers.calls == []

    @pytest.mark.parametrize(
        ("entry", "cwd", "reason"),
        [
            pytest.param(None, "/w", "pid 31337 is not in the process table.", id="missing_row"),
            pytest.param(
                row(31337, 115, "sleep 60", started="2026-09-30T06:30:00"),
                "/w",
                "pid 31337 was reparented: ppid 115 now, 27200 when tracked.",
                id="ppid",
            ),
            pytest.param(
                ProcessRow(31337, 27200, 27200, 501, datetime(2026, 9, 30, 6, 30), "sleep 60"),
                "/w",
                "pid 31337 changed group: pgid 27200 now, 31337 when tracked.",
                id="pgid",
            ),
            pytest.param(
                row(31337, 27200, "sleep 60", started="2026-09-30T06:30:01"),
                "/w",
                "pid 31337 was reused: its start time differs from the tracked process.",
                id="lstart",
            ),
            pytest.param(
                row(31337, 27200, "sleep  61", started="2026-09-30T06:30:00"),
                "/w",
                "pid 31337 exec'd a different program: `sleep  61` is not the tracked command.",
                id="argv",
            ),
            pytest.param(OWN_SLEEP, "/x", "pid 31337: cwd changed from `/w` to `/x`.", id="cwd_changed"),
            pytest.param(OWN_SLEEP, None, "pid 31337: cwd unreadable.", id="cwd_unreadable"),
        ],
    )
    def test_mismatch(self, entry: ProcessRow | None, cwd: str | None, reason: str) -> None:
        readers = Readers(entry, cwd)
        verdict = verify(identity_of(OWN_SLEEP), usage_row=readers.usage_row, process_cwd=readers.process_cwd)
        assert verdict == Unreadable(reason)

    def test_the_worker_compares_joined_command_text_not_the_argv_vector(self) -> None:
        readers = Readers(row(31337, 27200, "sleep   60", started="2026-09-30T06:30:00"), "/w")
        assert isinstance(
            verify(identity_of(OWN_SLEEP), usage_row=readers.usage_row, process_cwd=readers.process_cwd), ProcessRow
        )


class TestProve:
    def test_owns_a_child_of_this_sessions_agent(self) -> None:
        verdict = proved(MAC, identity_of(OWN_SLEEP))
        assert isinstance(verdict, Owned)
        assert verdict.row == OWN_SLEEP
        assert verdict.anchor == CLAUDE
        assert [entry.pid for entry in verdict.ancestry] == [27200, 14575]

    def test_owns_a_sibling_subtree_without_a_nested_agent(self) -> None:
        verdict = proved(NESTED, identity_of(NESTED.rows[40002]))
        assert isinstance(verdict, Owned)
        assert [entry.pid for entry in verdict.ancestry] == [27300, 14575]

    def test_refuses_a_child_of_a_nested_agent(self) -> None:
        assert refused(proved(NESTED, identity_of(NESTED.rows[40001]))) == (
            "pid 40001 runs under a nested agent (pid 40000), which owns it."
        )

    def test_refuses_a_protected_class_child(self) -> None:
        tmux = row(50000, 27200, "tmux new -s scratch", started="2026-09-30T06:31:00")
        assert refused(proved(with_rows(MAC, tmux), identity_of(tmux))) == (
            "pid 50000 is a terminal multiplexer, which no session may signal, stop, or restart."
        )

    def test_refuses_a_child_with_a_protected_descendant(self) -> None:
        shell = row(50001, 27200, "/bin/zsh -c tmux new -d", started="2026-09-30T06:31:00")
        tmux = row(50002, 50001, "tmux new -d", started="2026-09-30T06:31:01")
        assert refused(proved(with_rows(MAC, shell, tmux), identity_of(shell))) == (
            "pid 50002 is a terminal multiplexer, which no session may signal, stop, or restart."
        )

    def test_refuses_a_protected_descendant_deeper_than_the_old_walk_cap(self) -> None:
        shell = row(50001, 27200, "/bin/zsh -c wrapper", started="2026-09-30T06:31:00")
        links = chain(shell, DEEP, leaf="tmux new -d")
        assert refused(proved(with_rows(MAC, shell, *links), identity_of(shell))) == (
            f"pid {50001 + DEEP} is a terminal multiplexer, which no session may signal, stop, or restart."
        )

    def test_owns_a_deep_unprotected_subtree(self) -> None:
        shell = row(50001, 27200, "/bin/zsh -c wrapper", started="2026-09-30T06:31:00")
        links = chain(shell, DEEP)
        assert isinstance(proved(with_rows(MAC, shell, *links), identity_of(shell)), Owned)

    def test_refuses_a_deep_descendant_of_this_session_above_a_protected_leaf(self) -> None:
        shell = row(50001, 27200, "/bin/zsh -c wrapper", started="2026-09-30T06:31:00")
        links = chain(shell, DEEP, leaf="tmux new -d")
        snapshot = with_rows(MAC, shell, *links)
        middle = snapshot.rows[50001 + DEEP // 2]
        verdict = proved(snapshot, identity_of(middle))
        assert refused(verdict) == (
            f"pid {50001 + DEEP} is a terminal multiplexer, which no session may signal, stop, or restart."
        )
        assert [entry.pid for entry in snapshot.ancestors(50001 + DEEP)][-8:] == [
            50001,
            27200,
            14575,
            14550,
            14545,
            1743,
            1445,
            1,
        ]
        assert len(snapshot.ancestors(50001 + DEEP)) == DEEP + 7

    def test_refuses_a_child_with_an_agent_descendant(self) -> None:
        shell = row(50003, 27200, "/bin/zsh -c claude -p hi", started="2026-09-30T06:31:00")
        agent = row(50004, 50003, "claude -p hi", started="2026-09-30T06:31:01")
        assert refused(proved(with_rows(MAC, shell, agent), identity_of(shell))) == (
            "pid 50004 is an agent session, which no session may signal, stop, or restart."
        )

    def test_refuses_the_anchors_ancestor(self) -> None:
        assert refused(proved(MAC, identity_of(MAC.rows[14550]))) == (
            "pid 14550 is an ancestor of this session, which no session may signal."
        )

    def test_refuses_the_anchor_itself(self) -> None:
        assert refused(proved(MAC, identity_of(CLAUDE))) == (
            "pid 14575 is this session's own agent process, which no session may signal."
        )

    def test_refuses_pid_one(self) -> None:
        assert refused(proved(MAC, identity_of(MAC.rows[1]))) == "pid 1 is not a user process."

    def test_refuses_another_users_process(self) -> None:
        root = row(50005, 27200, "sleep 9", uid=0, started="2026-09-30T06:31:00")
        assert refused(proved(with_rows(MAC, root), identity_of(root))) == (
            f"pid 50005 belongs to uid 0, not this user (uid {os.getuid()})."
        )

    def test_refuses_a_process_outside_the_session(self) -> None:
        assert refused(proved(MAC, identity_of(MAC.rows[7777]))) == (
            "pid 7777 does not descend from this session's agent (pid 14575)."
        )

    def test_refuses_another_sessions_child(self) -> None:
        assert refused(proved(MAC, identity_of(MAC.rows[4242]))) == (
            "pid 4242 does not descend from this session's agent (pid 14575)."
        )

    @pytest.mark.parametrize(
        ("anchor", "reason"),
        [
            pytest.param(
                {"claude_start_unix": start_unix(CLAUDE) + 1},
                "pid 14575 is no longer this session's agent: its start time differs.",
                id="anchor_start",
            ),
            pytest.param(
                {"claude_pid": 99999}, "this session's agent (pid 99999) is not in the process table.", id="anchor_gone"
            ),
            pytest.param(
                {"claude_pid": 27200, "claude_start_unix": unix("2026-09-30T06:29:59")},
                "pid 27200 is not an agent process, so it cannot vouch for a child.",
                id="anchor_not_agent",
            ),
        ],
    )
    def test_refuses_an_unproven_anchor(self, anchor: dict[str, int], reason: str) -> None:
        assert refused(proved(MAC, identity_of(OWN_SLEEP), **anchor)) == reason

    def test_refuses_a_reused_pid(self) -> None:
        assert refused(proved(REUSED_PID, identity_of(OWN_SLEEP))) == (
            "pid 31337 was reparented: ppid 115 now, 27200 when tracked."
        )
        fresh = with_rows(
            table(*(entry for entry in MAC.rows.values() if entry.pid != OWN_SLEEP.pid)),
            row(31337, 27200, "sleep 60", started="2026-09-30T06:45:00"),
        )
        assert refused(proved(fresh, identity_of(OWN_SLEEP))) == (
            "pid 31337 was reused: its start time differs from the tracked process."
        )

    def test_refuses_an_exec(self) -> None:
        execd = with_rows(
            table(*(entry for entry in MAC.rows.values() if entry.pid != OWN_SLEEP.pid)),
            row(31337, 27200, "python3 -m http.server", started="2026-09-30T06:30:00"),
        )
        assert refused(proved(execd, identity_of(OWN_SLEEP))) == (
            "pid 31337 exec'd a different program: `python3 -m http.server` is not the tracked command."
        )

    def test_refuses_a_missing_child(self) -> None:
        assert refused(proved(MAC, ProcessIdentity(99998, 27200, 99998, 0, ("sleep", "1"), None))) == (
            "pid 99998 is not in the process table."
        )


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPT_HOOK_TEST_NO_LIVE")
    monkeypatch.setattr(sys, "platform", "darwin")


class TestTerminate:
    def test_refuses_live_signals_under_test(self) -> None:
        readers = Readers(OWN_SLEEP, "/w")
        assert readers.terminate(identity_of(OWN_SLEEP), signal.SIGTERM) == Outcome(
            False, "live signals are disabled under test"
        )
        assert readers.calls == []

    @pytest.mark.usefixtures("live")
    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["term", "kill"])
    def test_kills_exactly_once_after_cwd_recheck_and_identity(self, sig: signal.Signals) -> None:
        readers = Readers(OWN_SLEEP, "/w")
        assert readers.terminate(identity_of(OWN_SLEEP), sig) == Outcome(True, f"{sig.name} sent to pid 31337.")
        assert readers.calls == [("cwd", 31337), ("recheck",), ("row", 31337), ("kill", 31337, sig)]

    @pytest.mark.usefixtures("live")
    def test_an_identity_mismatch_sends_nothing(self) -> None:
        readers = Readers(row(31337, 27200, "sleep 60", started="2026-09-30T06:30:01"), "/w")
        outcome = readers.terminate(identity_of(OWN_SLEEP), signal.SIGTERM)
        assert outcome == Outcome(False, "pid 31337 was reused: its start time differs from the tracked process.")
        assert readers.calls == [("cwd", 31337), ("recheck",), ("row", 31337)]

    @pytest.mark.usefixtures("live")
    def test_a_failed_recheck_sends_nothing_and_skips_the_identity_read(self) -> None:
        readers = Readers(OWN_SLEEP, "/w", verdict=Unreadable("pid 31337 now runs under a nested agent."))
        outcome = readers.terminate(identity_of(OWN_SLEEP), signal.SIGTERM)
        assert outcome == Outcome(False, "pid 31337 now runs under a nested agent.")
        assert readers.calls == [("cwd", 31337), ("recheck",)]

    @pytest.mark.usefixtures("live")
    def test_a_missing_recorded_cwd_sends_nothing(self) -> None:
        readers = Readers(OWN_SLEEP, "/w")
        outcome = readers.terminate(identity_of(OWN_SLEEP, cwd=None), signal.SIGTERM)
        assert outcome == Outcome(False, "pid 31337: no recorded cwd to verify.")
        assert readers.calls == []

    @pytest.mark.usefixtures("live")
    def test_a_passed_deadline_sends_nothing(self) -> None:
        readers = Readers(OWN_SLEEP, "/w")
        expired = reqenv.RequestOverrides(
            env={}, cwd="/w", client_ppid=27103, session_id="s1", deadline_unix_ms=int(time.time() * 1000) - 1
        )
        with reqenv.use_request(expired):
            outcome = readers.terminate(identity_of(OWN_SLEEP), signal.SIGTERM)
        assert outcome == Outcome(False, "the dispatch deadline passed before the signal")
        assert readers.calls == [("cwd", 31337), ("recheck",), ("row", 31337)]

    @pytest.mark.usefixtures("live")
    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_the_signal_log_env_records_the_signal_instead_of_sending_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setenv("CAPT_HOOK_TEST_SIGNAL_LOG", str(log := tmp_path / "signals.log"))

        def pidfd_open(pid: int) -> int:
            pytest.fail(f"pidfd_open({pid}) under the sink")

        monkeypatch.setattr(os, "pidfd_open", pidfd_open, raising=False)
        readers = Readers(OWN_SLEEP, "/w")
        assert readers.terminate(identity_of(OWN_SLEEP), signal.SIGTERM) == Outcome(True, "SIGTERM sent to pid 31337.")
        assert log.read_text() == "31337 15\n"
        assert readers.calls == [("cwd", 31337), ("recheck",), ("row", 31337)]

    @pytest.mark.usefixtures("live")
    @pytest.mark.parametrize(
        ("exc", "reason"),
        [
            pytest.param(ProcessLookupError(), "exited before the signal", id="exited"),
            pytest.param(PermissionError(), "signal refused: permission", id="permission"),
        ],
    )
    def test_kill_errors_become_outcomes(self, exc: OSError, reason: str) -> None:
        readers = Readers(OWN_SLEEP, "/w")

        def kill(pid: int, sig: int) -> None:
            raise exc

        outcome = terminate(
            identity_of(OWN_SLEEP),
            signal.SIGTERM,
            recheck=readers.recheck,
            kill=kill,
            usage_row=readers.usage_row,
            process_cwd=readers.process_cwd,
        )
        assert outcome == Outcome(False, reason)

    @pytest.mark.usefixtures("live")
    @pytest.mark.parametrize(
        ("identity", "sig"),
        [
            pytest.param(identity_of(OWN_SLEEP), signal.SIGINT, id="other_signal"),
            pytest.param(identity_of(MAC.rows[1]), signal.SIGTERM, id="pid_one"),
        ],
    )
    def test_rejects_unsupported_targets(self, identity: ProcessIdentity, sig: int) -> None:
        readers = Readers(OWN_SLEEP, "/w")
        with pytest.raises(ValueError):
            readers.terminate(identity, sig)
        assert readers.calls == []

    def test_linux_signals_through_a_pidfd_opened_before_every_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CAPT_HOOK_TEST_NO_LIVE")
        monkeypatch.setattr(sys, "platform", "linux")
        readers = Readers(OWN_SLEEP, "/w")
        opened: list[int] = []

        def pidfd_open(pid: int) -> int:
            readers.calls.append(("pidfd_open", pid))
            opened.append(fd := os.open(os.devnull, os.O_RDONLY))
            return fd

        def pidfd_send_signal(fd: int, sig: int) -> None:
            readers.calls.append(("pidfd_send_signal", fd, sig))

        monkeypatch.setattr(os, "pidfd_open", pidfd_open, raising=False)
        monkeypatch.setattr(signal, "pidfd_send_signal", pidfd_send_signal, raising=False)
        assert readers.terminate(identity_of(OWN_SLEEP), signal.SIGTERM) == Outcome(True, "SIGTERM sent to pid 31337.")
        assert readers.calls == [
            ("pidfd_open", 31337),
            ("cwd", 31337),
            ("recheck",),
            ("row", 31337),
            ("pidfd_send_signal", opened[0], signal.SIGTERM),
        ]
        with pytest.raises(OSError):
            os.fstat(opened[0])

    def test_linux_pidfd_open_of_an_exited_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CAPT_HOOK_TEST_NO_LIVE")
        monkeypatch.setattr(sys, "platform", "linux")
        readers = Readers(OWN_SLEEP, "/w")

        def pidfd_open(pid: int) -> int:
            raise ProcessLookupError

        monkeypatch.setattr(os, "pidfd_open", pidfd_open, raising=False)
        assert readers.terminate(identity_of(OWN_SLEEP), signal.SIGTERM) == Outcome(False, "exited before the signal")
        assert readers.calls == []


class TestUsageRow:
    def test_parses_the_single_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(args: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert args == (*PS_ROW_ARGV, "31337")
            assert kwargs["env"] == os.environ | PS_TABLE_ENV
            assert kwargs["timeout"] == 1.5
            return subprocess.CompletedProcess(args, 0, stdout=ROW_LINE, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert usage_row(31337, timeout=1.5) == OWN_SLEEP

    @pytest.mark.parametrize(
        ("returncode", "stdout"),
        [
            pytest.param(1, "", id="no_such_pid"),
            pytest.param(0, "", id="empty"),
            pytest.param(0, ROW_LINE * 2, id="two_rows"),
            pytest.param(0, "garbage\n", id="malformed"),
        ],
    )
    def test_anything_but_one_row_is_none(self, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda args, **kw: subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=""),
        )
        assert usage_row(31337) is None

    def test_ps_failure_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired("ps", 2)

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert usage_row(31337) is None


class TestProcessCwd:
    def test_darwin_reads_the_lsof_name_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")

        def fake_run(args: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert args == (*LSOF_CWD_ARGV, "31337")
            assert kwargs["timeout"] == 2.0
            return subprocess.CompletedProcess(args, 0, stdout="p31337\nfcwd\nn/Users/y/Code with space\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert process_cwd(31337) == "/Users/y/Code with space"

    def test_darwin_without_a_name_line_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        )
        assert process_cwd(31337) is None

    def test_darwin_lsof_failure_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise OSError("no lsof")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert process_cwd(31337) is None

    def test_linux_reads_the_proc_link(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        seen: list[str] = []

        def readlink(path: str) -> str:
            seen.append(path)
            return "/srv/app"

        monkeypatch.setattr(os, "readlink", readlink)
        assert process_cwd(31337) == "/srv/app"
        assert seen == ["/proc/31337/cwd"]

    def test_linux_unreadable_link_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")

        def readlink(path: str) -> str:
            raise PermissionError(path)

        monkeypatch.setattr(os, "readlink", readlink)
        assert process_cwd(31337) is None


class TestChildren:
    def test_direct_children_only(self) -> None:
        assert {entry.pid for entry in children(MAC, 14575)} == {27103, 27200}
        assert children(MAC, 31337) == ()

    def test_a_self_parented_row_is_not_its_own_child(self) -> None:
        assert children(table(row(0, 0, "kernel_task", uid=0)), 0) == ()
