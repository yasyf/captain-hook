#!/usr/bin/env bash
set -euo pipefail

: "${APP_PATH:?APP_PATH is required}"

bridge="$APP_PATH/Contents/Helpers/capt-hook-helper-client"
app="$APP_PATH/Contents/MacOS/Captain Hook"
expected_team="SXKCTF23Q2"
expected_identifier="com.yasyf.capt-hook.helper.bridge"

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
  rm -rf "$state" "$entitlements"
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
