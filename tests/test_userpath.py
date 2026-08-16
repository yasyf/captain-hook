from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

from captain_hook.util import userpath

if TYPE_CHECKING:
    from pathlib import Path

LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def fake_shell(tmp_path: Path, body: str, *, exit_code: int = 0) -> str:
    """A shell stand-in for the passwd login shell: it ignores ``-l -c`` and prints ``body``."""
    (script := tmp_path / "login-shell").write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}EOF\nexit {exit_code}\n")
    script.chmod(0o755)
    return str(script)


def test_login_path_reads_the_exported_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(userpath, "login_shell", lambda: fake_shell(tmp_path, "/opt/tools/bin:/usr/bin\n"))
    assert userpath.login_path() == "/opt/tools/bin:/usr/bin"


def test_login_path_survives_a_chatty_login_banner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A login shell sources the user's profile, which may print before printenv answers.
    monkeypatch.setattr(userpath, "login_shell", lambda: fake_shell(tmp_path, "you have new mail\n/opt/tools/bin\n"))
    assert userpath.login_path() == "/opt/tools/bin"


@pytest.mark.parametrize(
    ("body", "exit_code"),
    [pytest.param("/opt/tools/bin\n", 1, id="nonzero-exit"), pytest.param("", 0, id="no-output")],
)
def test_login_path_raises_when_the_shell_will_not_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, exit_code: int
) -> None:
    monkeypatch.setattr(userpath, "login_shell", lambda: fake_shell(tmp_path, body, exit_code=exit_code))
    with pytest.raises(userpath.LoginShellError):
        userpath.login_path()


def test_login_path_raises_when_the_shell_cannot_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(userpath, "login_shell", lambda: str(tmp_path / "no-such-shell"))
    with pytest.raises(userpath.LoginShellError, match="could not be probed"):
        userpath.login_path()


def test_login_shell_comes_from_the_passwd_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # launchd sets no SHELL for a daemon, so the passwd entry is the only answer available.
    monkeypatch.delenv("SHELL", raising=False)
    assert os.path.isabs(userpath.login_shell())


def test_merged_path_puts_the_user_first_and_keeps_the_rest() -> None:
    merged = userpath.merged_path(LAUNCHD_PATH, "/opt/tools/bin:/usr/bin")
    assert merged == "/opt/tools/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def test_adopt_user_path_replaces_the_launchd_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.worker.__main__ import adopt_user_path

    (bindir := tmp_path / "tools").mkdir()
    (claude := bindir / "claude").write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    monkeypatch.setattr(userpath, "login_shell", lambda: fake_shell(tmp_path, f"{bindir}:{LAUNCHD_PATH}\n"))

    import shutil

    assert shutil.which("claude") is None  # the state every launchd-spawned worker starts in
    adopt_user_path()
    assert shutil.which("claude") == str(claude)


def test_adopt_user_path_records_a_fault_when_the_probe_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook import faults
    from captain_hook.worker.__main__ import adopt_user_path

    monkeypatch.setenv("PATH", LAUNCHD_PATH)
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

    assert update_argv()[:4] == [sys.executable, "-P", "-m", "captain_hook"]
