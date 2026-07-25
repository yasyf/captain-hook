import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
FORMULA = ROOT / ".github/formula/captain-hook.rb.tmpl"
SYSTEM_APPLICATION_GREP = (
    r"(^|[^$~[:alnum:]_])/Applications/Captain Hook\.app"
)


def _render_formula() -> str:
    return (
        FORMULA.read_text()
        .replace("__VERSION__", "12.20.2")
        .replace(
            "__ASSET_URL__",
            "https://github.com/yasyf/captain-hook/releases/download/"
            "v12.20.2/captain-hook-v12.20.2-darwin.zip",
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
    assert '"package-install"' in formula
    assert '$HOME/Applications/Captain Hook.app' in formula
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


def test_binrun_version_probes_use_the_formula_owned_host() -> None:
    expected = '["/usr/bin/env", "capt-hook-host", "version"]'
    for name in ("capt-hook.binrun", "hook.binrun"):
        descriptor = (ROOT / "captain_hook/bin" / name).read_text()
        assert expected in descriptor
        assert "/Applications/Captain Hook.app" not in descriptor
