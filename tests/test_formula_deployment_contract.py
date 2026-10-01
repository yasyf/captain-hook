from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from captain_hook.guard_literal import PASS_EXIT, deny_envelope
from tests.helpers import BENIGN_PAYLOAD, DESTRUCTIVE_PAYLOAD

ROOT = Path(__file__).parents[1]
FORMULA = ROOT / ".github/formula/captain-hook.rb.tmpl"
SYSTEM_APPLICATION_GREP = r"(^|[^$~[:alnum:]_])/Applications/Captain Hook\.app"
FAKE_HOST = '#!/bin/sh\necho "host $*"\ncat\n'
HOST_ECHO = "host run PreToolUse\n" + DESTRUCTIVE_PAYLOAD.decode()
ENVELOPES = {event: deny_envelope(event, "host-unavailable") + "\n" for event in ("PreToolUse", "PermissionRequest")}
STATIC_ENVELOPES = {
    event: deny_envelope(event, "dependency-unavailable") + "\n" for event in ("PreToolUse", "PermissionRequest")
}
BELOW_MINIMUM = "is version 12.65.1, want at least 12.66.0; run: brew upgrade yasyf/tap/captain-hook"
NOT_INSTALLED = "hook: Captain Hook is not installed; run: brew install yasyf/tap/captain-hook"
FAKE_BINRUN_REFUSING = f"#!/bin/sh\necho {shlex.quote(NOT_INSTALLED)} >&2\nexit 1\n"
FAKE_BINRUN_HEALTHY = "#!/bin/sh\nexit 0\n"
BROKEN_PYTHON = '#!/bin/sh\necho "Traceback (most recent call last):" >&2\nexit 1\n'
PASSING_PYTHON = f"#!/bin/sh\nexit {PASS_EXIT}\n"
REAL_PYTHON = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'
CHATTY_BROKEN_PYTHON = (
    '#!/bin/sh\necho "launcher: no interpreter found"\necho "Traceback (most recent call last):" >&2\nexit 1\n'
)
CHATTY_PASSING_PYTHON = f'#!/bin/sh\necho "launcher: resolved an interpreter"\nexit {PASS_EXIT}\n'
DENYING_PYTHON = f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(deny_envelope('PreToolUse', 'host-unavailable'))}\nexit 0\n"
STOCK_MACOS_PATH = "/usr/bin:/bin"
STOCK_MACOS_PYTHON = Path("/usr/bin/python3")


def _render_formula() -> str:
    return (
        FORMULA.read_text()
        .replace("__VERSION__", "12.20.2")
        .replace(
            "__ASSET_URL__",
            "https://github.com/yasyf/captain-hook/releases/download/v12.20.2/captain-hook-v12.20.2-darwin.zip",
        )
        .replace("__SHA_APP__", "a" * 64)
    )


def _grep_finds_system_application(formula: str) -> bool:
    result = subprocess.run(
        ["grep", "-Eq", SYSTEM_APPLICATION_GREP],
        input=formula,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1)
    return result.returncode == 0


def test_formula_bundles_and_applies_the_exact_signed_application() -> None:
    formula = FORMULA.read_text()
    assert 'libexec.install "Captain Hook.app"' in formula
    assert '"package-install"' not in formula
    assert "capt-hook helper install" in formula
    assert "$HOME/Applications/Captain Hook.app" in formula
    assert "--cask" not in formula
    user_scoped = formula.replace("$HOME/Applications/Captain Hook.app", "").replace(
        "~/Applications/Captain Hook.app", ""
    )
    assert "/Applications/Captain Hook.app" not in user_scoped


def test_shell_guard_accepts_user_paths_and_rejects_system_path() -> None:
    formula = _render_formula()

    assert "$HOME/Applications/Captain Hook.app" in formula
    assert "~/Applications/Captain Hook.app" in formula
    assert not _grep_finds_system_application(formula)

    system_formula = formula.replace(
        "$HOME/Applications/Captain Hook.app",
        "/Applications/Captain Hook.app",
    ).replace(
        "~/Applications/Captain Hook.app",
        "/Applications/Captain Hook.app",
    )
    assert _grep_finds_system_application(system_formula)


def test_signed_controller_stops_only_the_exact_installed_generation() -> None:
    source = (ROOT / "helper/Sources/App/ExactInstalledAppStop.swift").read_text()
    assert "NSRunningApplication.runningApplications(withBundleIdentifier:" in source
    assert "URL(fileURLWithPath: appPath" in source
    assert "application.bundleURL" in source
    assert "--stop-and-uninstall-service" not in source
    for forbidden in ("pkill", "pgrep", "killall", "osascript", "SMAppService"):
        assert forbidden not in source


def test_binrun_version_reads_the_stable_signed_host_without_spawning_it() -> None:
    """PIN: the version comes off the fixed signed bundle, and costs no execve to read.

    Spawning the host cost two execve per hook — a bash and a multi-MB signed Mach-O —
    to read a string the bundle already publishes, which on a machine whose endpoint
    security inspects every exec dominated the whole dispatch.
    """
    for name in ("capt-hook.binrun", "hook.binrun"):
        descriptor = (ROOT / "captain_hook/bin" / name).read_text()
        assert '"file": "~/Applications/Captain Hook.app/Contents/Info.plist"' in descriptor
        assert '"plist_key": "CFBundleShortVersionString"' in descriptor
        assert '"command"' not in descriptor
        assert "capt-hook-host" not in descriptor


def test_formula_never_runs_stapler_inside_the_homebrew_sandbox() -> None:
    assert "stapler" not in FORMULA.read_text()


def test_hook_dispatch_resolves_the_signed_host_not_python() -> None:
    """PIN: a hook event execs the signed host directly, with no Python interpreter in the chain.

    The ``capt_hook_client`` shim cost one execve per event; capt-hookd now spells Claude Code's
    ``run EVENT [--async]`` argv itself. Only dispatch moves: ``capt-hook`` is the full Python CLI.
    """
    hook = json.loads((ROOT / "captain_hook/bin/hook.binrun").read_text().split("\n", 1)[1])
    assert hook["kind"] == "signed-app"
    assert "tool" not in hook
    assert hook["app"] == {
        "dir": "~/Applications",
        "app_name": "Captain Hook",
        "exec": "Contents/Helpers/capt-hookd",
        "copy_exec": True,
        "formula": "yasyf/tap/captain-hook",
        "min_version": "12.66.0",
    }
    cli = json.loads((ROOT / "captain_hook/bin/capt-hook.binrun").read_text().split("\n", 1)[1])
    assert cli["kind"] == "python-tool"
    assert cli["tool"] == {"dist": "capt-hook", "entrypoint": "capt-hook"}


def test_linux_cli_reads_the_installed_host_version() -> None:
    """PIN: on Linux the CLI's tool env tracks the host ``package-install`` published, not a plist."""
    cli = json.loads((ROOT / "captain_hook/linux/bin/capt-hook.binrun").read_text().split("\n", 1)[1])
    assert cli["kind"] == "python-tool"
    assert cli["version"] == {"file": "~/.local/share/captain-hook/host/version.json", "json_field": "build"}
    assert cli["tool"] == {"dist": "capt-hook", "entrypoint": "capt-hook"}
    linux_cli = ROOT / "captain_hook/linux/bin/capt-hook"
    assert linux_cli.resolve() == (ROOT / "captain_hook/scripts/install-mcp.sh").resolve()


@pytest.mark.skipif(sys.platform != "linux", reason="the Linux hook shim")
@pytest.mark.parametrize(
    ("installed", "event", "payload", "code", "stdout"),
    [
        pytest.param(True, "PreToolUse", DESTRUCTIVE_PAYLOAD, 0, HOST_ECHO, id="installed-host-reads-stdin"),
        pytest.param(False, "PreToolUse", DESTRUCTIVE_PAYLOAD, 0, ENVELOPES["PreToolUse"], id="missing-host-denies"),
        pytest.param(
            False,
            "PermissionRequest",
            DESTRUCTIVE_PAYLOAD,
            0,
            ENVELOPES["PermissionRequest"],
            id="missing-host-denies-permission-request",
        ),
        pytest.param(False, "PreToolUse", BENIGN_PAYLOAD, 1, "", id="missing-host-fails-open-benign"),
        pytest.param(False, "Stop", DESTRUCTIVE_PAYLOAD, 1, "", id="missing-host-fails-open-unguarded-event"),
    ],
)
def test_linux_hook_dispatch_execs_the_installed_host_and_denies_without_it(
    installed: bool, event: str, payload: bytes, code: int, stdout: str, tmp_path: Path
) -> None:
    """PIN: a Linux hook event execs the host ``package-install`` placed with stdin untouched.

    Without one, a guarded call naming a session-ending program is denied; everything else
    fails open with bash's own error naming the missing host.
    """
    host = tmp_path / ".local/share/captain-hook/host/capt-hookd"
    if installed:
        host.parent.mkdir(parents=True)
        host.write_text(FAKE_HOST)
        host.chmod(0o755)
    result = subprocess.run(
        [ROOT / "captain_hook/bin/hook", "run", event],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        input=payload.decode(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode, result.stdout) == (code, stdout), result.stderr
    if not installed:
        assert str(host) in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="the signed-app hook shim")
@pytest.mark.parametrize(
    ("installed", "event", "payload", "code", "stdout", "hint"),
    [
        pytest.param(
            "12.65.1",
            "PreToolUse",
            DESTRUCTIVE_PAYLOAD,
            0,
            ENVELOPES["PreToolUse"],
            BELOW_MINIMUM,
            id="below-minimum-denies",
        ),
        pytest.param(
            "12.65.1",
            "PermissionRequest",
            DESTRUCTIVE_PAYLOAD,
            0,
            ENVELOPES["PermissionRequest"],
            BELOW_MINIMUM,
            id="below-minimum-denies-permission-request",
        ),
        pytest.param(
            "12.65.1", "PreToolUse", BENIGN_PAYLOAD, 1, "", BELOW_MINIMUM, id="below-minimum-fails-open-benign"
        ),
        pytest.param(
            None,
            "PreToolUse",
            DESTRUCTIVE_PAYLOAD,
            0,
            ENVELOPES["PreToolUse"],
            "Contents/Info.plist: no such file or directory",
            id="no-app-denies",
        ),
        pytest.param("12.66.0", "PreToolUse", DESTRUCTIVE_PAYLOAD, 0, HOST_ECHO, None, id="minimum-app-reads-stdin"),
        pytest.param("13.0.0", "PreToolUse", DESTRUCTIVE_PAYLOAD, 0, HOST_ECHO, None, id="newer-app-reads-stdin"),
    ],
)
def test_hook_dispatch_denies_a_guarded_call_binrun_refuses(
    installed: str | None, event: str, payload: bytes, code: int, stdout: str, hint: str | None, tmp_path: Path
) -> None:
    """PIN: binrun's refusal of a missing or too-old app denies a guarded call with its upgrade hint.

    binrun refuses before anything reads stdin, so the guard literal reads the payload intact and
    denies a session-ending call; a benign call keeps the non-blocking exit 1. An app at or past
    the minimum execs the host from the subshell with stdin untouched.
    """
    if installed is not None:
        contents = tmp_path / "Applications" / "Captain Hook.app" / "Contents"
        (contents / "Helpers").mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": installed}))
        host = contents / "Helpers" / "capt-hookd"
        host.write_text(FAKE_HOST)
        host.chmod(0o755)
    pinned = re.search(r'^RUNNER_TAG="(.+)"$', (ROOT / "captain_hook/scripts/install-binary.sh").read_text(), re.M)
    assert pinned is not None
    runner = Path.home() / ".daemonkit" / "binrun" / pinned[1] / "binrun"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "DAEMONKIT_HOME": str(tmp_path)}
    if runner.is_file():
        env["BINRUN_BIN"] = str(runner)
    result = subprocess.run(
        [ROOT / "captain_hook/bin/hook", "run", event],
        env=env,
        input=payload.decode(),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert (result.returncode, result.stdout) == (code, stdout), result.stderr
    if hint is not None:
        assert hint in result.stderr


def runner_env(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path), "DAEMONKIT_HOME": str(tmp_path), "BINRUN_BIN": str(tmp_path / "binrun")}


def fake_binrun(tmp_path: Path, script: str) -> dict[str, str]:
    (tmp_path / "binrun").write_text(script)
    (tmp_path / "binrun").chmod(0o755)
    return runner_env(tmp_path)


def unexecutable_binrun(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "binrun").write_text(FAKE_BINRUN_REFUSING)
    return runner_env(tmp_path)


def exec_failure_status(path: str) -> int:
    probe = subprocess.run(["/bin/bash", "-c", 'set -e; exec "$1"', "probe", path], capture_output=True, check=False)
    assert probe.returncode in (1, 126, 127)
    return probe.returncode


UNRUNNABLE_RUNNERS = [
    pytest.param(runner_env, id="missing-runner"),
    pytest.param(unexecutable_binrun, id="unexecutable-runner"),
]


def python3_path(tmp_path: Path, python3: str | None) -> Path:
    path = tmp_path / "path"
    path.mkdir()
    if python3 is not None:
        (path / "python3").write_text(python3)
        (path / "python3").chmod(0o755)
    return path


def linux_branch(tmp_path: Path, path: Path) -> dict[str, str]:
    return {"PATH": str(path), "HOME": str(tmp_path), "OSTYPE": "linux-gnu"}


def macos_arm(path: Path) -> dict[str, str]:
    (path / "id").symlink_to("/usr/bin/id")
    return {"PATH": str(path), "OSTYPE": "darwin"}


def macos_branch(tmp_path: Path, path: Path) -> dict[str, str]:
    return macos_arm(path) | fake_binrun(tmp_path, FAKE_BINRUN_REFUSING)


@pytest.mark.parametrize("branch", [pytest.param(linux_branch, id="linux"), pytest.param(macos_branch, id="macos")])
@pytest.mark.parametrize(
    ("python3", "event", "payload", "code", "stdout"),
    [
        pytest.param(
            None,
            "PreToolUse",
            DESTRUCTIVE_PAYLOAD,
            0,
            STATIC_ENVELOPES["PreToolUse"],
            id="no-python3-denies-destructive",
        ),
        pytest.param(
            None, "PreToolUse", BENIGN_PAYLOAD, 0, STATIC_ENVELOPES["PreToolUse"], id="no-python3-denies-benign"
        ),
        pytest.param(
            None,
            "PermissionRequest",
            DESTRUCTIVE_PAYLOAD,
            0,
            STATIC_ENVELOPES["PermissionRequest"],
            id="no-python3-denies-permission-request",
        ),
        pytest.param(None, "Stop", DESTRUCTIVE_PAYLOAD, 1, "", id="no-python3-fails-open-unguarded-event"),
        pytest.param(
            BROKEN_PYTHON,
            "PreToolUse",
            DESTRUCTIVE_PAYLOAD,
            0,
            STATIC_ENVELOPES["PreToolUse"],
            id="python3-cannot-run-the-literal-denies",
        ),
        pytest.param(PASSING_PYTHON, "PreToolUse", DESTRUCTIVE_PAYLOAD, 1, "", id="python3-pass-exit-fails-open"),
        pytest.param(
            REAL_PYTHON,
            "PreToolUse",
            DESTRUCTIVE_PAYLOAD,
            0,
            ENVELOPES["PreToolUse"],
            id="real-python3-classifier-denies",
        ),
        pytest.param(REAL_PYTHON, "PreToolUse", BENIGN_PAYLOAD, 1, "", id="real-python3-fails-open-benign"),
        pytest.param(
            CHATTY_BROKEN_PYTHON,
            "PreToolUse",
            DESTRUCTIVE_PAYLOAD,
            0,
            STATIC_ENVELOPES["PreToolUse"],
            id="python3-stdout-before-failing-is-dropped",
        ),
        pytest.param(
            CHATTY_PASSING_PYTHON,
            "PreToolUse",
            DESTRUCTIVE_PAYLOAD,
            1,
            "",
            id="python3-stdout-before-pass-exit-is-dropped",
        ),
        pytest.param(
            DENYING_PYTHON,
            "PreToolUse",
            DESTRUCTIVE_PAYLOAD,
            0,
            ENVELOPES["PreToolUse"],
            id="python3-verdict-is-published-verbatim",
        ),
    ],
)
def test_hook_dispatch_denies_statically_when_nothing_can_classify(
    branch: Callable[[Path, Path], dict[str, str]],
    python3: str | None,
    event: str,
    payload: bytes,
    code: int,
    stdout: str,
    tmp_path: Path,
) -> None:
    """PIN: with no host, a guarded event ends in the classifier's verdict; when no python3 can run the literal, it
    ends in the rendered dependency-unavailable envelope, on both the Linux and the signed-app branch of bin/hook.

    PATH is a temp directory holding only what the branch needs, so no python3 on the machine leaks in; an
    unguarded event keeps the non-blocking exit 1 either way. Only a classifier that exits 0 reaches stdout:
    whatever a launcher prints before failing or passing is dropped ahead of the static envelope.
    """
    result = subprocess.run(
        [ROOT / "captain_hook/bin/hook", "run", event],
        env=branch(tmp_path, python3_path(tmp_path, python3)),
        input=payload.decode(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode, result.stdout) == (code, stdout), result.stderr


@pytest.mark.parametrize("runner", UNRUNNABLE_RUNNERS)
@pytest.mark.parametrize(
    ("python3", "event", "payload", "code", "stdout"),
    [
        pytest.param(
            REAL_PYTHON,
            "PreToolUse",
            DESTRUCTIVE_PAYLOAD,
            0,
            ENVELOPES["PreToolUse"],
            id="real-python3-denies-destructive",
        ),
        pytest.param(
            REAL_PYTHON,
            "PermissionRequest",
            DESTRUCTIVE_PAYLOAD,
            0,
            ENVELOPES["PermissionRequest"],
            id="real-python3-denies-permission-request",
        ),
        pytest.param(REAL_PYTHON, "PreToolUse", BENIGN_PAYLOAD, 1, "", id="real-python3-fails-open-benign"),
        pytest.param(
            None,
            "PreToolUse",
            DESTRUCTIVE_PAYLOAD,
            0,
            STATIC_ENVELOPES["PreToolUse"],
            id="no-python3-denies-destructive",
        ),
        pytest.param(
            None, "PreToolUse", BENIGN_PAYLOAD, 0, STATIC_ENVELOPES["PreToolUse"], id="no-python3-denies-benign"
        ),
        pytest.param(
            None,
            "PermissionRequest",
            DESTRUCTIVE_PAYLOAD,
            0,
            STATIC_ENVELOPES["PermissionRequest"],
            id="no-python3-denies-permission-request",
        ),
    ],
)
def test_hook_dispatch_denies_when_the_chosen_runner_cannot_exec(
    runner: Callable[[Path], dict[str, str]],
    python3: str | None,
    event: str,
    payload: bytes,
    code: int,
    stdout: str,
    tmp_path: Path,
) -> None:
    """PIN: a BINRUN_BIN that is missing or not executable fails bash's exec before anything reads stdin, with a
    status that is not binrun's 1 on any bash past 3.2; a guarded event still ends in the classifier's verdict, or
    in the rendered dependency-unavailable envelope without a python3.
    """
    env = macos_arm(python3_path(tmp_path, python3)) | runner(tmp_path)
    result = subprocess.run(
        [ROOT / "captain_hook/bin/hook", "run", event],
        env=env,
        input=payload.decode(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode, result.stdout) == (code, stdout), result.stderr
    assert env["BINRUN_BIN"] in result.stderr


@pytest.mark.parametrize("runner", UNRUNNABLE_RUNNERS)
def test_hook_dispatch_keeps_the_failed_exec_status_for_an_unguarded_event(
    runner: Callable[[Path], dict[str, str]], tmp_path: Path
) -> None:
    """PIN: an unguarded event never reaches the deny path, so bash's own status for the failed exec is the whole
    result: what /bin/bash reports for a failed exec under the installer's errexit, 1 on bash 3.2 and 126 or 127
    on every later bash.
    """
    env = macos_arm(python3_path(tmp_path, None)) | runner(tmp_path)
    result = subprocess.run(
        [ROOT / "captain_hook/bin/hook", "run", "Stop"],
        env=env,
        input=DESTRUCTIVE_PAYLOAD.decode(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode, result.stdout) == (exec_failure_status(env["BINRUN_BIN"]), ""), result.stderr
    assert env["BINRUN_BIN"] in result.stderr
    assert "python3" not in result.stderr


@pytest.mark.parametrize("python3", [pytest.param(REAL_PYTHON, id="python3"), pytest.param(None, id="no-python3")])
def test_hook_dispatch_keeps_a_healthy_zero_off_the_deny_path(python3: str | None, tmp_path: Path) -> None:
    """PIN: a runner that exits 0 without reading stdin is a healthy dispatch, so the guarded subshell's 0 is the
    whole result: the deny path never probes the payload it left unread and no classifier publishes an envelope,
    python3 present or not.
    """
    env = macos_arm(python3_path(tmp_path, python3)) | fake_binrun(tmp_path, FAKE_BINRUN_HEALTHY)
    result = subprocess.run(
        [ROOT / "captain_hook/bin/hook", "run", "PreToolUse"],
        env=env,
        input=DESTRUCTIVE_PAYLOAD.decode(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
def test_linux_hook_dispatch_keeps_the_failed_exec_status_on_an_empty_stdin(event: str, tmp_path: Path) -> None:
    """PIN: with nothing on stdin the deny path's one-byte probe reads EOF, so the failed exec's own 126 or 127 is
    the result and no classifier runs.
    """
    host = tmp_path / ".local/share/captain-hook/host/capt-hookd"
    result = subprocess.run(
        [ROOT / "captain_hook/bin/hook", "run", event],
        env=linux_branch(tmp_path, python3_path(tmp_path, None)),
        input="",
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode in (126, 127), result.stderr
    assert result.stdout == ""
    assert str(host) in result.stderr
    assert "python3" not in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="the signed-app hook shim")
@pytest.mark.parametrize("python3", [pytest.param(REAL_PYTHON, id="python3"), pytest.param(None, id="no-python3")])
@pytest.mark.parametrize(
    ("host_exit", "code"),
    [pytest.param(2, 2, id="host-block-verdict"), pytest.param(1, 1, id="host-error")],
)
def test_hook_dispatch_passes_the_host_verdict_through_the_guarded_subshell(
    host_exit: int, code: int, python3: str | None, tmp_path: Path
) -> None:
    """PIN: a guarded event's subshell returns the host's own exit code, including the blocking 2.

    Every nonzero status reaches the deny path, whose one-byte probe finds the stdin a host that
    ran has spent and hands that host's status back untouched: no envelope follows and no python3
    runs, present or not.
    """
    host = tmp_path / "capt-hookd"
    host.write_text(FAKE_HOST + f"exit {host_exit}\n")
    host.chmod(0o755)
    exec_host = f'#!/bin/sh\nshift\nexec {shlex.quote(str(host))} "$@"\n'
    path = python3_path(tmp_path, python3)
    for tool in ("/usr/bin/id", "/bin/cat"):
        (path / Path(tool).name).symlink_to(tool)
    env = {"PATH": str(path)} | fake_binrun(tmp_path, exec_host)
    result = subprocess.run(
        [ROOT / "captain_hook/bin/hook", "run", "PreToolUse"],
        env=env,
        input=DESTRUCTIVE_PAYLOAD.decode(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode, result.stdout, result.stderr) == (code, HOST_ECHO, "")


@pytest.mark.skipif(sys.platform != "darwin" or not STOCK_MACOS_PYTHON.is_file(), reason="the stock macOS python3")
@pytest.mark.parametrize(
    ("event", "payload", "code", "stdout"),
    [
        pytest.param("PreToolUse", DESTRUCTIVE_PAYLOAD, 0, ENVELOPES["PreToolUse"], id="denies"),
        pytest.param("PermissionRequest", DESTRUCTIVE_PAYLOAD, 0, ENVELOPES["PermissionRequest"], id="denies-pr"),
        pytest.param("PreToolUse", BENIGN_PAYLOAD, 1, "", id="fails-open-benign"),
    ],
)
def test_hook_dispatch_denies_under_the_stock_macos_python(
    event: str, payload: bytes, code: int, stdout: str, tmp_path: Path
) -> None:
    """PIN: with only the stock /usr/bin/python3 on PATH, binrun's refusal still ends in the deny envelope."""
    env = {"PATH": STOCK_MACOS_PATH} | fake_binrun(tmp_path, FAKE_BINRUN_REFUSING)
    result = subprocess.run(
        [ROOT / "captain_hook/bin/hook", "run", event],
        env=env,
        input=payload.decode(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode, result.stdout, result.stderr) == (code, stdout, NOT_INSTALLED + "\n")


def test_hook_dispatch_shrugs_off_an_inherited_errexit(tmp_path: Path) -> None:
    """PIN: an exported SHELLOPTS=errexit cannot end bin/hook at the failed dispatch before the literal runs."""
    env = {"PATH": os.environ["PATH"], "SHELLOPTS": "errexit"} | fake_binrun(tmp_path, FAKE_BINRUN_REFUSING)
    result = subprocess.run(
        [ROOT / "captain_hook/bin/hook", "run", "PreToolUse"],
        env=env,
        input=DESTRUCTIVE_PAYLOAD.decode(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode, result.stdout) == (0, ENVELOPES["PreToolUse"]), result.stderr
