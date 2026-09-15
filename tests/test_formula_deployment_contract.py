import json
import os
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FORMULA = ROOT / ".github/formula/captain-hook.rb.tmpl"
SYSTEM_APPLICATION_GREP = r"(^|[^$~[:alnum:]_])/Applications/Captain Hook\.app"


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
    for name in ("capt-hook.binrun", "hook.binrun", "hook-shim.binrun"):
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
        "formula": "yasyf/tap/captain-hook",
        "min_version": "12.29.0",
    }
    cli = json.loads((ROOT / "captain_hook/bin/capt-hook.binrun").read_text().split("\n", 1)[1])
    assert cli["kind"] == "python-tool"
    assert cli["tool"] == {"dist": "capt-hook", "entrypoint": "capt-hook"}
    shim = json.loads((ROOT / "captain_hook/bin/hook-shim.binrun").read_text().split("\n", 1)[1])
    assert shim["kind"] == "python-tool"
    assert shim["tool"] == {"dist": "capt-hook", "entrypoint": "hook"}
    assert (ROOT / "captain_hook/bin/hook-shim").readlink() == Path("../scripts/install-hook-shim.sh")


@pytest.mark.parametrize(
    ("installed", "code", "output"),
    [
        ("12.28.0", 1, "is version 12.28.0, want at least 12.29.0; run: brew upgrade yasyf/tap/captain-hook"),
        ("12.29.0", 0, "host run PreToolUse"),
        ("13.0.0", 0, "host run PreToolUse"),
    ],
)
def test_hook_dispatch_below_the_minimum_app_fails_open_with_the_upgrade_hint(
    installed: str, code: int, output: str, tmp_path: Path
) -> None:
    """PIN: an app older than the Go hook grammar would exit 2 on ``run <Event>``, blocking every tool.

    The descriptor's ``min_version`` stops binrun before that exec: exit 1 does not block, and its
    stderr names the upgrade. An app at or past the minimum execs the host.
    """
    contents = tmp_path / "Applications" / "Captain Hook.app" / "Contents"
    (contents / "Helpers").mkdir(parents=True)
    plistlib.dump({"CFBundleShortVersionString": installed}, (contents / "Info.plist").open("wb"))
    host = contents / "Helpers" / "capt-hookd"
    host.write_text('#!/bin/sh\necho "host $*"\n')
    host.chmod(0o755)
    pinned = re.search(r'^RUNNER_TAG="(.+)"$', (ROOT / "captain_hook/scripts/install-binary.sh").read_text(), re.M)
    assert pinned is not None
    runner = Path.home() / ".daemonkit" / "binrun" / pinned[1] / "binrun"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "DAEMONKIT_HOME": str(tmp_path)}
    if runner.is_file():
        env["BINRUN_BIN"] = str(runner)
    result = subprocess.run(
        [ROOT / "captain_hook/bin/hook", "run", "PreToolUse"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == code, result.stderr
    assert output in (result.stdout if code == 0 else result.stderr)
