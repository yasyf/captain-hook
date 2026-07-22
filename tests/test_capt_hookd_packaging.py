import json
import platform
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(platform.system() != "Darwin", reason="universal Mach-O packaging requires lipo")
def test_builder_emits_exact_versioned_universal_host(tmp_path: Path) -> None:
    output = tmp_path / "capt-hookd"
    subprocess.run(
        [
            ROOT / "helper/scripts/build-capt-hookd.sh",
            "12.9.1-rc.2",
            output,
            ROOT / "tests/fixtures/capt-hookd",
        ],
        check=True,
    )

    archs = set(subprocess.run(["lipo", "-archs", output], check=True, capture_output=True, text=True).stdout.split())
    assert archs == {"arm64", "x86_64"}
    version = subprocess.run([output, "version"], check=True, capture_output=True, text=True).stdout
    assert json.loads(version) == {"schema": 1, "build": "12.9.1-rc.2"}


def test_release_contract_signs_and_verifies_embedded_host() -> None:
    project = (ROOT / "helper/project.yml").read_text()
    assertion = (ROOT / "helper/scripts/assert-signed-bridge.sh").read_text()
    workflow = (ROOT / ".github/workflows/release-pypi.yml").read_text()

    assert "Generated/capt-hookd" in project
    assert "CaptHookBuild: $(GITHUB_REF_NAME)" in project
    assert "subpath: Contents/Helpers" in project
    assert "CodeSignOnCopy" in project
    assert "Contents/Helpers/capt-hookd" in assertion
    assert 'lipo -archs "$host"' in assertion
    assert 'codesign --verify --strict --verbose=2 "$host"' in assertion
    assert '"$host" version' in assertion
    assert 'test "$app_build" = "v$release_version"' in assertion
    assert (
        "github.com/yasyf/captain-hook/internal/hookd.Build=$version"
        in (ROOT / "helper/scripts/build-capt-hookd.sh").read_text()
    )
    assert "${release_version%%-*}" in assertion
    assert "changed_paths:" not in workflow
    assert "needs.helper.outputs.changed" not in workflow
    assert 'v="${GITHUB_REF_NAME#v}"' in workflow
    assert "${GITHUB_REF_NAME#v}" in (ROOT / "helper/scripts/build-capt-hookd.sh").read_text()
