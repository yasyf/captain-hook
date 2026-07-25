#!/usr/bin/env bash
# Build, sign, and install "Captain Hook.app" locally with a Developer ID
# (never ad-hoc: identity churn resets the notification-permission grant).
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"

version="${1:-1.0.0}"
identity="${CODESIGN_IDENTITY:-Developer ID Application}"
team="${DEVELOPMENT_TEAM:-SXKCTF23Q2}"
app_name="Captain Hook"

./scripts/make-appicon.sh
./scripts/build-capt-hookd.sh "$version"
./gen-version-xcconfig.sh "$version"
xcodegen generate

derived="$(mktemp -d)"
GITHUB_REF_NAME="v$version" xcodebuild -scheme CaptainHook -configuration Release \
  -derivedDataPath "$derived" \
  CODE_SIGN_STYLE=Manual \
  CODE_SIGN_IDENTITY="$identity" \
  DEVELOPMENT_TEAM="$team" \
  ENABLE_HARDENED_RUNTIME=YES \
  CODE_SIGN_INJECT_BASE_ENTITLEMENTS=NO \
  build

app="$derived/Build/Products/Release/$app_name.app"

# Assert the App Group entitlement and the stamped version survived signing.
codesign -d --entitlements :- "$app" 2>/dev/null | grep -q 'com.yasyf.capt-hook.helper'
plutil -extract CFBundleShortVersionString raw "$app/Contents/Info.plist"
CAPT_HOOK_VERSION="$version" APP_PATH="$app" bash scripts/assert-signed-bridge.sh

# Install through the same exact daemonkit deployment transaction as the formula.
"$app/Contents/Helpers/capt-hookd" package-install

echo "installed $HOME/Applications/$app_name.app"
