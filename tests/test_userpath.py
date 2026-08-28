from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from captain_hook.util import userpath

if TYPE_CHECKING:
    from pathlib import Path

LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# A budget for tests that spawn a real shell. The production constant is a policy about hookd's
# readiness handshake, and an endpoint-security agent can put seconds on any exec of /bin/sh.
PROBE_BUDGET = 60


def tagged(path: str) -> str:
    return f"{userpath.PATH_TAG}{path}\n"


def fake_shell(tmp_path: Path, body: str, *, exit_code: int = 0) -> str:
    """A shell stand-in for the passwd login shell: it ignores ``-l -c`` and prints ``body``."""
    (script := tmp_path / "login-shell").write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}EOF\nexit {exit_code}\n")
    script.chmod(0o755)
    return str(script)


def test_login_path_reads_the_exported_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(userpath, "login_shell", lambda: fake_shell(tmp_path, tagged("/opt/tools/bin:/usr/bin")))
    assert userpath.login_path(PROBE_BUDGET) == "/opt/tools/bin:/usr/bin"


def test_login_path_survives_a_chatty_login_banner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A login shell sources the user's profile, which may print before printenv answers.
    body = f"you have new mail\n{tagged('/opt/tools/bin')}"
    monkeypatch.setattr(userpath, "login_shell", lambda: fake_shell(tmp_path, body))
    assert userpath.login_path(PROBE_BUDGET) == "/opt/tools/bin"


def test_login_path_survives_logout_chatter_after_the_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # zsh runs .zlogout after the -c command, so the answer is not the last line either.
    body = f"{tagged('/opt/tools/bin')}session closed\n"
    monkeypatch.setattr(userpath, "login_shell", lambda: fake_shell(tmp_path, body))
    assert userpath.login_path(PROBE_BUDGET) == "/opt/tools/bin"


def test_login_path_runs_the_real_pipeline_in_a_real_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub shells prove the parsing; this proves the command string. path_helper rebuilds a login
    # shell's PATH, so the marker is carried rather than kept in place.
    monkeypatch.setattr(userpath, "login_shell", lambda: "/bin/sh")
    monkeypatch.setenv("PATH", f"/opt/marker/bin:{LAUNCHD_PATH}")
    entries = userpath.login_path(PROBE_BUDGET).split(os.pathsep)
    assert "/opt/marker/bin" in entries
    assert "/usr/bin" in entries


def test_login_path_does_not_read_the_callers_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An rc file with an unguarded `read` would otherwise eat hookd's length-prefixed hello frame.
    import subprocess

    seen: dict[str, object] = {}
    real = subprocess.run

    def spy(*args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(userpath, "login_shell", lambda: fake_shell(tmp_path, tagged("/opt/tools/bin")))
    monkeypatch.setattr(subprocess, "run", spy)
    userpath.login_path(PROBE_BUDGET)
    assert seen["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    ("body", "exit_code"),
    [pytest.param("/opt/tools/bin\n", 1, id="nonzero-exit"), pytest.param("", 0, id="no-output")],
)
def test_login_path_raises_when_the_shell_will_not_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, exit_code: int
) -> None:
    monkeypatch.setattr(userpath, "login_shell", lambda: fake_shell(tmp_path, body, exit_code=exit_code))
    with pytest.raises(userpath.LoginShellError):
        userpath.login_path(PROBE_BUDGET)


def test_login_path_raises_when_the_shell_cannot_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(userpath, "login_shell", lambda: str(tmp_path / "no-such-shell"))
    with pytest.raises(userpath.LoginShellError, match="could not be probed"):
        userpath.login_path(PROBE_BUDGET)


def test_login_shell_comes_from_the_passwd_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # launchd sets no SHELL for a daemon, so the passwd entry is the only answer available.
    monkeypatch.delenv("SHELL", raising=False)
    assert os.path.isabs(userpath.login_shell())


def test_merged_path_puts_the_user_first_and_keeps_the_rest() -> None:
    merged = userpath.merged_path(LAUNCHD_PATH, "/opt/tools/bin:/usr/bin")
    assert merged == "/opt/tools/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@pytest.mark.parametrize(
    "login",
    [
        pytest.param(":/opt/tools/bin", id="leading-empty"),
        pytest.param("/opt/tools/bin:", id="trailing-empty"),
        pytest.param("/opt/tools/bin::/usr/bin", id="interior-empty"),
        pytest.param(".:/opt/tools/bin", id="dot"),
        pytest.param("relative/bin:/opt/tools/bin", id="relative"),
    ],
)
def test_merged_path_drops_entries_that_would_search_the_session_repo(login: str) -> None:
    # POSIX resolves an empty entry against cwd, and a worker's cwd is the repository under review —
    # so an executable committed there must never answer a bare `claude` lookup.
    merged = userpath.merged_path(LAUNCHD_PATH, login)
    assert all(entry and os.path.isabs(entry) for entry in merged.split(os.pathsep))
    assert "/opt/tools/bin" in merged.split(os.pathsep)


def test_merged_path_drops_unsafe_inherited_entries_too() -> None:
    assert userpath.merged_path(f".:{LAUNCHD_PATH}", "/opt/tools/bin") == f"/opt/tools/bin:{LAUNCHD_PATH}"


def test_adopt_user_path_replaces_the_launchd_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.worker.__main__ import adopt_user_path

    (bindir := tmp_path / "tools").mkdir()
    (claude := bindir / "claude").write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    monkeypatch.setattr(userpath, "PROBE_TIMEOUT_SECONDS", PROBE_BUDGET)
    monkeypatch.setattr(userpath, "cache_file", lambda: tmp_path / "login-path.json")
    monkeypatch.setattr(userpath, "login_shell", lambda: fake_shell(tmp_path, tagged(f"{bindir}:{LAUNCHD_PATH}")))

    import shutil

    assert shutil.which("claude") is None  # the state every launchd-spawned worker starts in
    adopt_user_path()
    assert shutil.which("claude") == str(claude)


def test_adopt_user_path_records_a_fault_when_the_probe_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook import faults
    from captain_hook.worker.__main__ import adopt_user_path

    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    monkeypatch.setattr(userpath, "cache_file", lambda: tmp_path / "login-path.json")
    monkeypatch.setattr(userpath, "login_shell", lambda: str(tmp_path / "no-such-shell"))

    adopt_user_path()

    assert os.environ["PATH"] == LAUNCHD_PATH
    (line,) = faults.drain()
    assert "login shell PATH probe" in line


def test_dash_p_keeps_the_cwd_off_sys_path(tmp_path: Path) -> None:
    # The mechanism behind the -P on the worker and reviewer argv: a directory that shares an
    # installed dependency's name shadows it when the interpreter starts with the cwd on sys.path.
    import subprocess

    (shadow := tmp_path / "filelock").mkdir()
    (shadow / "__init__.py").write_text("raise ImportError('shadowed')\n")

    shadowed = subprocess.run(
        [sys.executable, "-c", "import filelock"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert shadowed.returncode != 0 and "shadowed" in shadowed.stderr

    isolated = subprocess.run(
        [sys.executable, "-P", "-c", "import filelock"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert isolated.returncode == 0, isolated.stderr


def test_reviewer_spawn_is_import_isolated() -> None:
    from captain_hook.review.pipeline import spawn_argv

    assert spawn_argv("/t.jsonl", "/repo")[:4] == [sys.executable, "-P", "-m", "captain_hook"]


def test_update_spawn_is_import_isolated() -> None:
    # updater.detach() spawns from the session's repo too, so it needs the same isolation.
    from captain_hook.update.updater import update_argv

    assert update_argv(apply=True)[:4] == [sys.executable, "-P", "-m", "captain_hook"]


def recording_probe(value: str, timeouts: list[float]) -> object:
    """A ``login_path`` stand-in that records the budget each call was given."""

    def probe(timeout: float = userpath.PROBE_TIMEOUT_SECONDS) -> str:
        timeouts.append(timeout)
        return value

    return probe


def stale(shell: str, path: str) -> str:
    aged = datetime.now(UTC) - userpath.CACHE_TTL - timedelta(minutes=1)
    return json.dumps({"shell": shell, "path": path, "at": aged.isoformat()})


def test_user_path_probes_once_and_then_reads_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []
    monkeypatch.setattr(userpath, "cache_file", lambda: tmp_path / "login-path.json")
    monkeypatch.setattr(userpath, "login_shell", lambda: "/bin/fish")
    monkeypatch.setattr(userpath, "login_path", recording_probe("/opt/tools/bin", timeouts))

    assert userpath.user_path() == "/opt/tools/bin"
    assert userpath.user_path() == "/opt/tools/bin"
    assert timeouts == [userpath.PROBE_TIMEOUT_SECONDS]


def test_user_path_refreshes_a_stale_record_on_the_shorter_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A refresh that fails still has the stale answer behind it, so it may not spend the cold budget.
    (cache := tmp_path / "login-path.json").write_text(stale("/bin/fish", "/opt/old/bin"))
    timeouts: list[float] = []
    monkeypatch.setattr(userpath, "cache_file", lambda: cache)
    monkeypatch.setattr(userpath, "login_shell", lambda: "/bin/fish")
    monkeypatch.setattr(userpath, "login_path", recording_probe("/opt/tools/bin", timeouts))

    assert userpath.user_path() == "/opt/tools/bin"
    assert timeouts == [userpath.REFRESH_TIMEOUT_SECONDS]


def test_user_path_falls_back_to_a_stale_record_when_the_refresh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The failure this fixes: a login shell that answers late blows hookd's readiness handshake, so
    # a worker that already has an answer must never be lost to one.
    (cache := tmp_path / "login-path.json").write_text(stale("/bin/fish", "/opt/tools/bin"))
    monkeypatch.setattr(userpath, "cache_file", lambda: cache)
    monkeypatch.setattr(userpath, "login_shell", lambda: "/bin/fish")
    monkeypatch.setattr(
        userpath, "login_path", lambda timeout=0: (_ for _ in ()).throw(userpath.LoginShellError("late"))
    )

    assert userpath.user_path() == "/opt/tools/bin"


def test_user_path_raises_when_the_probe_fails_with_nothing_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(userpath, "cache_file", lambda: tmp_path / "login-path.json")
    monkeypatch.setattr(userpath, "login_shell", lambda: str(tmp_path / "no-such-shell"))
    with pytest.raises(userpath.LoginShellError):
        userpath.user_path()


def test_user_path_ignores_a_record_written_by_another_login_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = json.dumps({"shell": "/bin/other", "path": "/opt/stale/bin", "at": datetime.now(UTC).isoformat()})
    (cache := tmp_path / "login-path.json").write_text(fresh)
    timeouts: list[float] = []
    monkeypatch.setattr(userpath, "cache_file", lambda: cache)
    monkeypatch.setattr(userpath, "login_shell", lambda: "/bin/fish")
    monkeypatch.setattr(userpath, "login_path", recording_probe("/opt/tools/bin", timeouts))

    assert userpath.user_path() == "/opt/tools/bin"
    assert timeouts == [userpath.PROBE_TIMEOUT_SECONDS]


def test_user_path_survives_a_corrupt_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (cache := tmp_path / "login-path.json").write_text("{not json")
    monkeypatch.setattr(userpath, "cache_file", lambda: cache)
    monkeypatch.setattr(userpath, "login_shell", lambda: "/bin/fish")
    monkeypatch.setattr(userpath, "login_path", recording_probe("/opt/tools/bin", []))

    assert userpath.user_path() == "/opt/tools/bin"


def test_adopt_user_path_takes_the_cached_answer_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point: a worker that finds a fresh record never runs a login shell before readiness.
    from captain_hook.worker.__main__ import adopt_user_path

    (bindir := tmp_path / "tools").mkdir()
    (claude := bindir / "claude").write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    record = {"shell": "/bin/fish", "path": f"{bindir}:{LAUNCHD_PATH}", "at": datetime.now(UTC).isoformat()}
    (cache := tmp_path / "login-path.json").write_text(json.dumps(record))
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    monkeypatch.setattr(userpath, "cache_file", lambda: cache)
    monkeypatch.setattr(userpath, "login_shell", lambda: "/bin/fish")
    monkeypatch.setattr(userpath, "login_path", lambda timeout=0: pytest.fail("the login shell was probed"))

    import shutil

    adopt_user_path()
    assert shutil.which("claude") == str(claude)
