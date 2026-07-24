import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_cask_requires_exact_stop_hook_and_rejects_name_based_control() -> None:
    cask = (ROOT / ".github/cask/captain-hook.rb.tmpl").read_text()
    assert cask.count('args: ["--stop-and-uninstall-service"]') == 2
    assert cask.count("must_succeed: true") >= 2
    assert cask.count("if File.executable?(installed_binary)") == 2
    guarded = re.findall(r"if File\.executable\?\(installed_binary\)(.*?)\n\s*(?:elsif|end)\b", cask, re.S)
    assert len(guarded) == 2
    assert all('args: ["--stop-and-uninstall-service"]' in block for block in guarded)
    assert all("must_succeed: true" in block for block in guarded)
    assert "elsif Dir.exist?(installed_app)" in cask
    assert "FileUtils.rm_rf(installed_app)" in cask
    lower = cask.lower()
    for forbidden in ("pkill", "pgrep", "killall", "osascript", "uninstall quit:"):
        assert forbidden not in lower


def test_product_hook_uses_exact_service_and_stable_bundle_apis() -> None:
    source = (ROOT / "helper/Sources/App/ExactAppServiceStop.swift").read_text()
    assert "SMAppService.agent" in source
    assert "NSRunningApplication.runningApplications(withBundleIdentifier:" in source
    assert "Bundle.main.bundleURL" in source
    assert "application.bundleURL" in source
    for forbidden in ("pkill", "pgrep", "killall", "osascript"):
        assert forbidden not in source.lower()
