from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/release-pypi.yml"
CASK = ROOT / ".github/cask/captain-hook.rb.tmpl"
RELEASE_APP_REF = "19c3d5013032ad9c88f9a8f1170d1f366c19b8d9"
HOME_BREW_ACTION_REF = "19c3d5013032ad9c88f9a8f1170d1f366c19b8d9"


def test_helper_release_uses_exact_hard_cut_contract() -> None:
    workflow = WORKFLOW.read_text()
    helper = workflow[workflow.index("\n  helper:") : workflow.index("\n  helper-cask:")]

    assert f"release-app.yml@{RELEASE_APP_REF}" in helper
    assert "asset_name: captain-hook" in helper
    assert "go_version: 1.26.5" in helper
    assert "prebuild_script: helper/scripts/build-capt-hookd.sh" in helper
    assert "changed_paths:" not in helper
    assert "cask_token:" not in helper
    assert "cask_template_path:" not in helper
    assert "HOMEBREW_TAP_TOKEN:" not in helper


def test_release_is_published_only_after_helper_and_python_artifacts_are_verified() -> None:
    workflow = WORKFLOW.read_text()
    publish = workflow[workflow.index("\n  github-release:") : workflow.index("\n  sync-plugin-version:")]

    for required in (
        "needs: [build, helper, publish, sync-plugin-version, verify-release-artifacts]",
        "needs.helper.outputs.artifact_name",
        "needs.helper.outputs.asset_filename",
        "needs.helper.outputs.sha256",
        'gh release create "$TAG"',
        "--draft --title",
        'gh release upload "$TAG"',
        'gh release edit "$TAG"',
        "--draft=false",
    ):
        assert required in publish

    assert publish.index("--draft") < publish.index("gh release upload")
    assert publish.index("gh release upload") < publish.index("--draft=false")


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
