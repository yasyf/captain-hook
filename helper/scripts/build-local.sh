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
./gen-version-xcconfig.sh "$version"
xcodegen generate

derived="$(mktemp -d)"
xcodebuild -scheme CaptainHook -configuration Release \
  -derivedDataPath "$derived" \
  CODE_SIGN_STYLE=Manual \
  CODE_SIGN_IDENTITY="$identity" \
  DEVELOPMENT_TEAM="$team" \
  build

app="$derived/Build/Products/Release/$app_name.app"

# Assert the App Group entitlement and the stamped version survived signing.
codesign -d --entitlements :- "$app" 2>/dev/null | grep -q 'com.yasyf.capt-hook.helper'
plutil -extract CFBundleShortVersionString raw "$app/Contents/Info.plist"

# Install: boot the old login item out, replace the app, relaunch detached.
pkill -x "$app_name" || true
[ -d "/Applications/$app_name.app" ] && "/Applications/$app_name.app/Contents/MacOS/$app_name" --unregister || true
ditto "$app" "/Applications/$app_name.app"
open -g "/Applications/$app_name.app"

echo "installed /Applications/$app_name.app"
