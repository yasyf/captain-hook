import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SYSTEM_APPLICATION = re.compile(
    r"(^|[^$~A-Za-z0-9_])/Applications/Captain Hook\.app"
)


def test_formula_bundles_and_applies_the_exact_signed_application() -> None:
    formula = (ROOT / ".github/formula/captain-hook.rb.tmpl").read_text()
    assert 'libexec.install "Captain Hook.app"' in formula
    assert '"package-install"' in formula
    assert '$HOME/Applications/Captain Hook.app' in formula
    assert "--cask" not in formula
    user_scoped = formula.replace("$HOME/Applications/Captain Hook.app", "").replace(
        "~/Applications/Captain Hook.app", ""
    )
    assert "/Applications/Captain Hook.app" not in user_scoped


def test_system_application_guard_does_not_reject_user_applications() -> None:
    assert SYSTEM_APPLICATION.search("/Applications/Captain Hook.app")
    assert SYSTEM_APPLICATION.search("at /Applications/Captain Hook.app")
    assert not SYSTEM_APPLICATION.search("$HOME/Applications/Captain Hook.app")
    assert not SYSTEM_APPLICATION.search("~/Applications/Captain Hook.app")


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
