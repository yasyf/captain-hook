from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/release-pypi.yml"
FORMULA = ROOT / ".github/formula/captain-hook.rb.tmpl"
RELEASE_APP_REF = "7cc8a6c981cbec10fcb7f19bd75b36e9ee65ea7e"
STAGE_DRAFT_RELEASE_REF = "e4c3108e693681df1a3c666bae80e890bc44cf3e"
PUBLISH_DRAFT_RELEASE_REF = "54e3e194bda69896894a82c17fcdb2822beefab5"
HOME_BREW_ACTION_REF = "19c3d5013032ad9c88f9a8f1170d1f366c19b8d9"
TAP_PUBLISH_REF = "9525763796fce4d1042cf3393d9479f791908eaa"
PYPI_PUBLISH_REF = "ba38be9e461d3875417946c167d0b5f3d385a247"


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


def test_release_stages_and_smokes_every_asset_before_one_public_transition() -> None:
    workflow = WORKFLOW.read_text()
    release_tests = workflow[workflow.index("\n  release-tests:") : workflow.index("\n  build:")]
    build = workflow[workflow.index("\n  build:") : workflow.index("\n  python-assets:")]
    python_assets = workflow[workflow.index("\n  python-assets:") : workflow.index("\n  helper-version:")]
    stage = workflow[workflow.index("\n  stage-release:") : workflow.index("\n  smoke-draft:")]
    smoke = workflow[workflow.index("\n  smoke-draft:") : workflow.index("\n  # PyPI Trusted Publishing")]
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

    for required in (
        "needs: [build, python-assets, helper]",
        "name: ${{ needs.helper.outputs.artifact_name }}",
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
    assert "bash helper/scripts/assert-signed-bridge.sh" in smoke
    assert "RELEASE_ID:" not in smoke
    assert "GH_TOKEN:" not in smoke
    assert "gh api" not in smoke
    assert "contents: write" not in smoke

    for required in (
        "needs: [build, stage-release, smoke-draft]",
        "name: release-assets",
        "Resolve the exact PyPI publication state",
        "existing PyPI assets differ",
        "steps.pypi-state.outputs.publish == 'true'",
        "Verify exact PyPI publication",
        'manifest="$RUNNER_TEMP/captain-pypi-assets.tsv"',
        'printf \'%s\\t%s\\n\' "$staged_sha" "$asset" >> "$manifest"',
        f"pypa/gh-action-pypi-publish@{PYPI_PUBLISH_REF}",
    ):
        assert required in publish_pypi
    assert "id-token: write" in publish_pypi
    assert "contents: write" not in publish_pypi
    assert "skip-existing:" not in publish_pypi
    assert publish_pypi.count('"$RUNNER_TEMP/captain-pypi-assets.tsv"') == 3
    assert "pathlib.Path(directory).iterdir()" not in publish_pypi
    assert "if path.is_file()" not in publish_pypi
    assert "pypa/gh-action-pypi-publish@release/v1" not in workflow
    assert publish_pypi.index(f"pypa/gh-action-pypi-publish@{PYPI_PUBLISH_REF}") < publish_pypi.index(
        "Verify exact PyPI publication"
    )

    assert "needs: [build, stage-release, publish-pypi]" in publish_github
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
    assert '"package-install"' in formula
    assert '$HOME/Applications/Captain Hook.app' in formula
    assert "\n  cask " not in formula


def test_formula_release_guard_allows_only_user_scoped_applications() -> None:
    workflow = WORKFLOW.read_text()
    formula_job = workflow[workflow.index("\n  helper-formula:") :]

    assert r"(^|[^$~[:alnum:]_])/Applications/Captain Hook\.app" in formula_job
    assert r"(^|[^$[:alnum:]_])/Applications/Captain Hook\.app" not in formula_job
