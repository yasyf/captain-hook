#!/usr/bin/env bash
set -euo pipefail

: "${APP_PATH:?APP_PATH is required}"

bridge="$APP_PATH/Contents/Helpers/capt-hook-helper-client"
host="$APP_PATH/Contents/Helpers/capt-hookd"
app="$APP_PATH/Contents/MacOS/Captain Hook"
expected_team="SXKCTF23Q2"
expected_identifier="com.yasyf.capt-hook.helper.bridge"
release_version="${CAPT_HOOK_VERSION:-${GITHUB_REF_NAME#v}}"

test -n "$release_version"
test -x "$host"
host_archs=" $(lipo -archs "$host") "
[[ "$host_archs" == *" arm64 "* ]]
[[ "$host_archs" == *" x86_64 "* ]]
test "$(wc -w <<< "$host_archs" | tr -d ' ')" = 2
codesign --verify --strict --verbose=2 "$host"
host_details="$(codesign -dvvv "$host" 2>&1)"
grep -q "^TeamIdentifier=$expected_team$" <<< "$host_details"
grep -q '(runtime)' <<< "$host_details"
host_version="$("$host" version)"
python3 - "$release_version" "$host_version" <<'PY'
import json
import sys

expected, raw = sys.argv[1:]
assert json.loads(raw) == {"schema": 1, "build": expected}
PY
host_entitlements="$(mktemp)"
if ! codesign -d --entitlements :- "$host" > "$host_entitlements" 2>/dev/null; then
  : > "$host_entitlements"
fi
for key in \
  com.apple.security.get-task-allow \
  com.apple.security.cs.allow-dyld-environment-variables \
  com.apple.security.cs.allow-jit \
  com.apple.security.cs.allow-unsigned-executable-memory \
  com.apple.security.cs.disable-executable-page-protection \
  com.apple.security.cs.disable-library-validation; do
  if grep -q "$key" "$host_entitlements"; then
    echo "host carries forbidden entitlement $key" >&2
    exit 1
  fi
done

test -x "$bridge"
codesign --verify --strict --verbose=2 "$bridge"
details="$(codesign -dvvv "$bridge" 2>&1)"
grep -q "^Identifier=$expected_identifier$" <<< "$details"
grep -q "^TeamIdentifier=$expected_team$" <<< "$details"
grep -q '(runtime)' <<< "$details"

entitlements="$(mktemp)"
if ! codesign -d --entitlements :- "$bridge" > "$entitlements" 2>/dev/null; then
  : > "$entitlements"
fi
for key in \
  com.apple.security.get-task-allow \
  com.apple.security.cs.allow-dyld-environment-variables \
  com.apple.security.cs.allow-jit \
  com.apple.security.cs.allow-unsigned-executable-memory \
  com.apple.security.cs.disable-executable-page-protection \
  com.apple.security.cs.disable-library-validation; do
  if grep -q "$key" "$entitlements"; then
    echo "bridge carries forbidden entitlement $key" >&2
    exit 1
  fi
done

state="$(mktemp -d /tmp/capt-hook-bridge.XXXXXX)"
app_log="$state/app.log"
cleanup() {
  if [[ -n "${app_pid:-}" ]]; then
    kill "$app_pid" 2>/dev/null || true
    wait "$app_pid" 2>/dev/null || true
  fi
  rm -rf "$state" "$entitlements" "$host_entitlements"
}
trap cleanup EXIT

CAPT_HOOK_HELPER_DIR="$state" "$app" > "$app_log" 2>&1 &
app_pid=$!

for _ in $(seq 1 40); do
  if ping="$("$bridge" --socket "$state/helper.sock" ping 2>/dev/null)"; then
    break
  fi
  kill -0 "$app_pid" 2>/dev/null || { cat "$app_log" >&2; exit 1; }
  sleep 0.25
done
: "${ping:?signed bridge ping did not complete}"

version="$(plutil -extract CFBundleShortVersionString raw "$APP_PATH/Contents/Info.plist")"
test "$version" = "${release_version%%-*}"
app_build="$(plutil -extract CaptHookBuild raw "$APP_PATH/Contents/Info.plist")"
test "$app_build" = "v$release_version"
python3 - "$version" "$ping" <<'PY'
import json
import sys

version, raw = sys.argv[1:]
assert json.loads(raw) == {"ok": True, "version": version}
PY

notify="$("$bridge" --socket "$state/helper.sock" notify <<'JSON'
{"kind":"pr_open","title":"Captain Hook signed bridge","subtitle":"release assertion","body":"typed notify session","url":"https://github.com/yasyf/captain-hook","repo":"github.com/yasyf/captain-hook"}
JSON
)"
python3 - "$notify" <<'PY'
import json
import sys

assert json.loads(sys.argv[1]) == {"ok": True}
PY
