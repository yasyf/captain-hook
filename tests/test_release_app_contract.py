from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import textwrap
from graphlib import TopologicalSorter
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/release-pypi.yml"
FORMULA = ROOT / ".github/formula/captain-hook.rb.tmpl"
RELEASE_APP_REF = "9ddbb1310b476b1cd617d9f8e5563c6c295cf6c7"
STAGE_DRAFT_RELEASE_REF = "e4c3108e693681df1a3c666bae80e890bc44cf3e"
PUBLISH_DRAFT_RELEASE_REF = "54e3e194bda69896894a82c17fcdb2822beefab5"
HOME_BREW_ACTION_REF = "19c3d5013032ad9c88f9a8f1170d1f366c19b8d9"
TAP_PUBLISH_REF = "9525763796fce4d1042cf3393d9479f791908eaa"
PYPI_PUBLISH_REF = "dc37677b2e1c63e2034f94d8a5b11f265b73ba33"


def workflow_job(workflow: str, name: str) -> str:
    return re.split(r"(?m)^  \S", workflow.split(f"\n  {name}:\n", 1)[1], maxsplit=1)[0]


def workflow_step_script(job: str, name: str) -> str:
    step = job.split(f"      - name: {name}\n", 1)[1]
    return textwrap.dedent(re.split(r"(?m)^      - ", step.split("        run: |\n", 1)[1], maxsplit=1)[0])


def test_helper_release_uses_exact_hard_cut_contract() -> None:
    workflow = WORKFLOW.read_text()
    helper = workflow[workflow.index("\n  helper:") : workflow.index("\n  helper-formula:")]

    assert f"release-app.yml@{RELEASE_APP_REF}" in helper
    assert "asset_name: captain-hook" in helper
    assert "go_version: 1.26.5" in helper
    assert "prebuild_script: helper/scripts/build-capt-hookd.sh" in helper
    assert "needs: [build, python-assets, helper-version]" in helper
    assert "contents: read" in helper
    assert "changed_paths:" not in helper
    assert "cask_token:" not in helper
    assert "cask_template_path:" not in helper
    assert "HOMEBREW_TAP_TOKEN:" not in helper


def test_release_stages_and_smokes_every_app_asset_before_github_publication() -> None:
    workflow = WORKFLOW.read_text()
    release_tests = workflow[workflow.index("\n  release-tests:") : workflow.index("\n  build:")]
    build = workflow[workflow.index("\n  build:") : workflow.index("\n  python-assets:")]
    python_assets = workflow[workflow.index("\n  python-assets:") : workflow.index("\n  helper-version:")]
    linux_host = workflow[workflow.index("\n  linux-host:") : workflow.index("\n  stage-release:")]
    stage = workflow[workflow.index("\n  stage-release:") : workflow.index("\n  smoke-draft:")]
    smoke = workflow[workflow.index("\n  smoke-draft:") : workflow.index("\n  publish-pypi:")]
    publish_pypi = workflow[workflow.index("\n  publish-pypi:") : workflow.index("\n  publish-github:")]
    publish_github = workflow[workflow.index("\n  publish-github:") : workflow.index("\n  # Consumer plugin caches")]
    sync = workflow[workflow.index("\n  sync-plugin-version:") : workflow.index("\n  helper-formula:")]
    formula = workflow[workflow.index("\n  helper-formula:") :]

    assert "uses: ./.github/actions/python-tests" in release_tests
    assert "needs: release-tests" in build
    assert "check-version: false" in build
    assert "run-tests: false" in build
    assert "name: dist" in python_assets
    assert 'test "${#assets[@]}" = 2' in python_assets
    assert "Smoke-test the built wheel" in python_assets
    assert "name: verified-python-assets" in python_assets
    assert "path: verified-python/*" in python_assets
    assert python_assets.index("Smoke-test the built wheel") < python_assets.index("Record the verified Python asset manifest")

    assert "needs: build" in linux_host
    assert "CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build" in linux_host
    assert "internal/hookd.Build=$version" in linux_host
    assert 'tar -C linux-host -czf "captain-hook-$TAG-linux-amd64.tar.gz" capt-hookd' in linux_host

    for required in (
        "needs: [build, python-assets, helper, linux-host]",
        "name: verified-python-assets",
        'cmp python-artifacts/captain-pypi-assets.tsv "$manifest"',
        "name: ${{ needs.helper.outputs.artifact_name }}",
        "name: linux-host",
        'linux_asset="captain-hook-${RELEASE_TAG}-linux-amd64.tar.gz"',
        'helper_sidecar="${helper_assets[0]}.sha256"',
        'sha256sum -c "$HELPER_ASSET_FILENAME.sha256"',
        "SHA256SUMS.txt",
        "name: release-assets",
        "release_id: ${{ steps.draft.outputs['release-id'] }}",
        f"stage-draft-release@{STAGE_DRAFT_RELEASE_REF}",
        "manifest: ${{ runner.temp }}/captain-release-assets",
        "prerelease: ${{ contains(needs.build.outputs.tag, '-') }}",
        "name: staged-release-${{ steps.draft.outputs['release-id'] }}",
        "path: ${{ steps.draft.outputs['download-dir'] }}/*",
    ):
        assert required in stage
    assert "needs: [build, helper, stage-release]" in smoke
    assert "name: staged-release-${{ needs.stage-release.outputs.release_id }}" in smoke
    assert "path: released" in smoke
    assert "Smoke-test the final staged application bytes" in smoke
    assert "xcrun stapler validate" in smoke
    assert 'xcrun stapler validate "$app" || status=$?' in smoke
    assert 'if [ "$status" -ne 68 ] || [ "$attempt" -ge 6 ]; then' in smoke
    assert "bash helper/scripts/assert-signed-bridge.sh" in smoke
    assert "RELEASE_ID:" not in smoke
    assert "GH_TOKEN:" not in smoke
    assert "gh api" not in smoke
    assert "contents: write" not in smoke

    for required in (
        "needs: [build, python-assets, stage-release, smoke-draft]",
        "name: verified-python-assets",
        "Resolve the exact PyPI publication state",
        "existing PyPI assets differ",
        "steps.pypi-state.outputs.publish == 'true'",
        "Verify exact PyPI publication",
        'manifest="$RUNNER_TEMP/captain-pypi-assets.tsv"',
        'printf \'%s\\t%s\\n\' "$python_sha" "$asset" >> "$manifest"',
        'cmp python-artifacts/captain-pypi-assets.tsv "$manifest"',
        f"pypa/gh-action-pypi-publish@{PYPI_PUBLISH_REF}",
    ):
        assert required in publish_pypi
    assert "id-token: write" in publish_pypi
    assert "contents: write" not in publish_pypi
    assert "skip-existing:" not in publish_pypi
    assert "name: release-assets" not in publish_pypi
    assert publish_pypi.count('"$RUNNER_TEMP/captain-pypi-assets.tsv"') == 3
    assert "pathlib.Path(directory).iterdir()" not in publish_pypi
    assert "if path.is_file()" not in publish_pypi
    assert "pypa/gh-action-pypi-publish@release/v1" not in workflow
    assert publish_pypi.index(f"pypa/gh-action-pypi-publish@{PYPI_PUBLISH_REF}") < publish_pypi.index(
        "Verify exact PyPI publication"
    )

    assert "needs: [build, stage-release, smoke-draft, publish-pypi]" in publish_github
    assert "contents: write" in publish_github
    assert "id-token: write" not in publish_github
    assert "Publish the exact already-complete GitHub draft" in publish_github
    assert f"publish-draft-release@{PUBLISH_DRAFT_RELEASE_REF}" in publish_github
    assert "release-id: ${{ needs.stage-release.outputs.release_id }}" in publish_github
    assert "make-latest: ${{ !contains(needs.build.outputs.tag, '-') }}" in publish_github

    assert "needs: publish-github" in sync
    assert "needs: [helper-version, helper, publish-github, sync-plugin-version]" in formula
    draft_flow = stage + smoke + publish_github
    assert "gh release view" not in draft_flow
    assert "gh release upload" not in draft_flow
    assert "gh release download" not in draft_flow
    assert "gh release edit" not in draft_flow
    assert "gh api" not in smoke
    assert "/releases/tags/" not in draft_flow
    assert "softprops/action-gh-release" not in workflow


@pytest.mark.parametrize("blocked", ["helper", "smoke-draft"])
def test_python_publication_waits_for_the_matching_app_smoke(blocked: str) -> None:
    workflow = WORKFLOW.read_text()
    jobs = re.findall(r"(?m)^  ([a-z][a-z-]+):$", workflow)
    dependencies = {}
    for name in jobs:
        needs = re.search(r"(?m)^    needs: ([^\n]+)$", workflow_job(workflow, name))
        dependencies[name] = set(needs[1].strip("[]").split(", ")) if needs else set()
    assert set.union(*dependencies.values()) <= set(jobs)
    assert dependencies["python-assets"] == {"build"}
    assert dependencies["build"] == {"release-tests"}
    assert "smoke-draft" in dependencies["publish-github"]

    graph = TopologicalSorter(dependencies)
    graph.prepare()
    completed = set()
    while ready := graph.get_ready():
        finished = set(ready) - {blocked}
        graph.done(*finished)
        completed |= finished

    assert {"release-tests", "build", "python-assets"} <= completed
    assert not {"publish-pypi", "publish-github", "sync-plugin-version", "helper-formula"} & completed
    assert dependencies["publish-pypi"] == {"build", "python-assets", "stage-release", "smoke-draft"}
    assert ("stage-release" in completed) == (blocked == "smoke-draft")


@pytest.mark.skipif(sys.platform != "linux", reason="The release asset scripts run on Ubuntu")
@pytest.mark.parametrize("consumer", ["stage-release", "publish-pypi"])
@pytest.mark.parametrize("tamper", ["unchanged", "changed-wheel", "missing-sdist", "extra-file", "changed-manifest", "missing-manifest"])
def test_python_manifest_joins_the_actual_publication_scripts(tmp_path: Path, consumer: str, tamper: str) -> None:
    workflow = WORKFLOW.read_text()
    producer = tmp_path / "producer"
    (producer / "dist").mkdir(parents=True)
    packages = {"capt_hook-12.15.3-py3-none-any.whl": b"wheel bytes", "capt_hook-12.15.3.tar.gz": b"source bytes"}
    for name, payload in packages.items():
        (producer / "dist" / name).write_bytes(payload)
    runner = tmp_path / "runner"
    runner.mkdir()
    env = os.environ | {"RUNNER_TEMP": str(runner)}
    record = workflow_step_script(workflow_job(workflow, "python-assets"), "Record the verified Python asset manifest")
    subprocess.run(["bash", "-c", record], cwd=producer, env=env, check=True, capture_output=True, text=True)
    artifact = producer / "verified-python"
    manifest_name = "captain-pypi-assets.tsv"
    assert {path.name for path in artifact.iterdir()} == set(packages) | {manifest_name}
    assert (artifact / manifest_name).read_text() == "".join(
        f"{hashlib.sha256(packages[name]).hexdigest()}\t{name}\n" for name in sorted(packages)
    )

    work = tmp_path / "consumer"
    work.mkdir()
    source = work / "python-artifacts"
    shutil.copytree(artifact, source)
    match tamper:
        case "changed-wheel":
            (source / "capt_hook-12.15.3-py3-none-any.whl").write_bytes(b"different wheel")
        case "missing-sdist":
            (source / "capt_hook-12.15.3.tar.gz").unlink()
        case "extra-file":
            (source / "unexpected.txt").write_text("extra")
        case "changed-manifest":
            (source / manifest_name).write_text("different manifest\n")
        case "missing-manifest":
            (source / manifest_name).unlink()

    script = (
        workflow_step_script(workflow_job(workflow, consumer), "Assemble the exact complete release").split("mapfile -t helper_assets", 1)[0]
        if consumer == "stage-release"
        else workflow_step_script(workflow_job(workflow, consumer), "Match the PyPI upload to the verified Python manifest")
    )
    result = subprocess.run(["bash", "-c", script], cwd=work, env=env, capture_output=True, text=True)
    if tamper != "unchanged":
        assert result.returncode != 0, result.stdout + result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    destination = work / ("dist" if consumer == "stage-release" else "python-dist")
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == packages


def test_pypi_publisher_sidecars_cannot_mutate_expected_assets(tmp_path: Path) -> None:
    workflow = WORKFLOW.read_text()
    publish = workflow[workflow.index("\n  publish-pypi:") : workflow.index("\n  publish-github:")]
    wheel = "capt_hook-12.15.3-py3-none-any.whl"
    source = "capt_hook-12.15.3.tar.gz"
    manifest = tmp_path / "captain-pypi-assets.tsv"
    manifest.write_text(f"{'1' * 64}\t{wheel}\n{'2' * 64}\t{source}\n")
    expected = {name: digest for digest, name in (line.split("\t", 1) for line in manifest.read_text().splitlines())}

    (tmp_path / f"{wheel}.publish.attestation").write_text("publisher output")
    (tmp_path / f"{source}.publish.attestation").write_text("publisher output")

    assert expected == {wheel: "1" * 64, source: "2" * 64}
    publisher = publish.index(f"pypa/gh-action-pypi-publish@{PYPI_PUBLISH_REF}")
    verifier = publish.index("Verify exact PyPI publication")
    assert publish.index('manifest="$RUNNER_TEMP/captain-pypi-assets.tsv"') < publisher < verifier
    assert "python-dist" not in publish[verifier:]
    assert "pathlib.Path(manifest).read_text()" in publish[verifier:]


def test_release_rerun_converges_the_unique_draft_by_release_id() -> None:
    workflow = WORKFLOW.read_text()
    stage = workflow[workflow.index("\n  stage-release:") : workflow.index("\n  smoke-draft:")]
    publish_github = workflow[workflow.index("\n  publish-github:") : workflow.index("\n  # Consumer plugin caches")]

    assert f"stage-draft-release@{STAGE_DRAFT_RELEASE_REF}" in stage
    assert f"publish-draft-release@{PUBLISH_DRAFT_RELEASE_REF}" in publish_github
    assert "release_id: ${{ steps.draft.outputs['release-id'] }}" in stage
    assert "release-id: ${{ needs.stage-release.outputs.release_id }}" in publish_github
    assert "/releases?per_page=" not in stage
    assert "releases/assets/" not in stage
    assert "--method PATCH" not in publish_github


def test_formula_publication_uses_verified_release_outputs() -> None:
    workflow = WORKFLOW.read_text()
    formula_job = workflow[workflow.index("\n  helper-formula:") :]

    for required in (
        "needs.helper.outputs.asset_filename",
        "needs.helper.outputs.asset_url",
        "needs.helper.outputs.sha256",
        "Verify the final distributed application bytes",
        "awk 'NR == 1 { print $1 }' \"$ASSET_FILENAME.sha256\"",
        'shasum -a 256 "$ASSET_FILENAME"',
        "Guard formula registry name and version",
        "Require the bundled signed application formula",
        "__ASSET_URL__=${{ needs.helper.outputs.asset_url }}",
        "__SHA_APP__=${{ needs.helper.outputs.sha256 }}",
        f"render-formula@{HOME_BREW_ACTION_REF}",
        f"publish@{TAP_PUBLISH_REF}",
    ):
        assert required in formula_job

    assert formula_job.count("homebrew-tap/.github/actions/publish@") == 1
    assert "homebrew-tap/.github/actions/publish@v" not in formula_job
    assert "shasum -a 256 -c" not in formula_job


def test_formula_uses_authoritative_asset_and_bundled_application() -> None:
    formula = FORMULA.read_text()

    assert 'url "__ASSET_URL__", using: :nounzip' in formula
    assert "/releases/download/" not in formula
    assert 'libexec.install "Captain Hook.app"' in formula
    assert '"package-install"' not in formula
    assert "capt-hook helper install" in formula
    assert "$HOME/Applications/Captain Hook.app" in formula
    assert "\n  cask " not in formula


def test_formula_release_guard_allows_only_user_scoped_applications() -> None:
    workflow = WORKFLOW.read_text()
    formula_job = workflow[workflow.index("\n  helper-formula:") :]

    assert r"(^|[^$~[:alnum:]_])/Applications/Captain Hook\.app" in formula_job
    assert r"(^|[^$[:alnum:]_])/Applications/Captain Hook\.app" not in formula_job
