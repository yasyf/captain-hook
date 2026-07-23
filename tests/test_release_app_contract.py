from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/release-pypi.yml"
CASK = ROOT / ".github/cask/captain-hook.rb.tmpl"
RELEASE_APP_REF = "8f422c652d836c40f9cc5a9d893d4120b26bc681"
HOME_BREW_ACTION_REF = "19c3d5013032ad9c88f9a8f1170d1f366c19b8d9"
PYPI_PUBLISH_REF = "ba38be9e461d3875417946c167d0b5f3d385a247"


def test_helper_release_uses_exact_hard_cut_contract() -> None:
    workflow = WORKFLOW.read_text()
    helper = workflow[workflow.index("\n  helper:") : workflow.index("\n  helper-cask:")]

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
    build = workflow[workflow.index("\n  build:") : workflow.index("\n  python-assets:")]
    python_assets = workflow[workflow.index("\n  python-assets:") : workflow.index("\n  helper-version:")]
    stage = workflow[workflow.index("\n  stage-release:") : workflow.index("\n  smoke-draft:")]
    smoke = workflow[workflow.index("\n  smoke-draft:") : workflow.index("\n  # PyPI Trusted Publishing")]
    publish_pypi = workflow[workflow.index("\n  publish-pypi:") : workflow.index("\n  publish-github:")]
    publish_github = workflow[workflow.index("\n  publish-github:") : workflow.index("\n  # Consumer plugin caches")]
    sync = workflow[workflow.index("\n  sync-plugin-version:") : workflow.index("\n  helper-cask:")]
    cask = workflow[workflow.index("\n  helper-cask:") :]

    assert "check-version: false" in build
    assert "run-tests: true" in build
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
        "--generate-notes --draft --verify-tag",
        'gh release upload "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" dist/* --clobber',
        "release assets do not exactly match the verified dist",
    ):
        assert required in stage
    assert "--draft=false" not in stage

    assert "needs: [build, helper, stage-release]" in smoke
    assert "Smoke-test the final staged application bytes" in smoke
    assert "xcrun stapler validate" in smoke
    assert "bash helper/scripts/assert-signed-bridge.sh" in smoke

    for required in (
        "needs: [build, stage-release, smoke-draft]",
        "name: release-assets",
        "skip-existing: true",
        "Verify exact PyPI publication",
        f"pypa/gh-action-pypi-publish@{PYPI_PUBLISH_REF}",
    ):
        assert required in publish_pypi
    assert "id-token: write" in publish_pypi
    assert "contents: write" not in publish_pypi
    assert "pypa/gh-action-pypi-publish@release/v1" not in workflow
    assert publish_pypi.index(f"pypa/gh-action-pypi-publish@{PYPI_PUBLISH_REF}") < publish_pypi.index(
        "Verify exact PyPI publication"
    )

    assert "needs: [build, stage-release, publish-pypi]" in publish_github
    assert "contents: write" in publish_github
    assert "id-token: write" not in publish_github
    assert "Publish the already-complete GitHub draft" in publish_github
    assert 'gh release edit "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" --draft=false' in publish_github

    assert "needs: publish-github" in sync
    assert "needs: [helper-version, helper, publish-github, sync-plugin-version]" in cask
    assert workflow.count("gh release create") == 1
    assert workflow.count("--draft=false") == 1
    assert "/releases/tags/" not in workflow
    assert "softprops/action-gh-release" not in workflow


def test_cask_publication_uses_verified_release_outputs() -> None:
    workflow = WORKFLOW.read_text()
    cask_job = workflow[workflow.index("\n  helper-cask:") :]

    for required in (
        "needs.helper.outputs.asset_filename",
        "needs.helper.outputs.asset_url",
        "needs.helper.outputs.sha256",
        "Verify the final distributed application bytes",
        "awk 'NR == 1 { print $1 }' \"$ASSET_FILENAME.sha256\"",
        'shasum -a 256 "$ASSET_FILENAME"',
        "Guard cask registry name and version",
        "Require a full-application cask template",
        "__ASSET_URL__=${{ needs.helper.outputs.asset_url }}",
        "__SHA_APP__=${{ needs.helper.outputs.sha256 }}",
        f"render-formula@{HOME_BREW_ACTION_REF}",
        f"publish@{HOME_BREW_ACTION_REF}",
    ):
        assert required in cask_job

    assert cask_job.count("homebrew-tap/.github/actions/publish@") == 1
    assert "homebrew-tap/.github/actions/publish@v" not in cask_job
    assert "shasum -a 256 -c" not in cask_job


def test_cask_uses_authoritative_asset_url_and_full_application() -> None:
    cask = CASK.read_text()

    assert 'url "__ASSET_URL__"' in cask
    assert "/releases/download/" not in cask
    assert '\n  app "Captain Hook.app"' in cask
    assert "\n  binary " not in cask
