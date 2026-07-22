#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
version="${1:-${CAPT_HOOK_VERSION:-${GITHUB_REF_NAME#v}}}"
output="${2:-$repo_root/helper/Generated/capt-hookd}"
module_root="${3:-$repo_root}"

if ! [[ "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$ ]]; then
  echo "build-capt-hookd.sh: version '$version' is not strict semantic version" >&2
  exit 2
fi
test -f "$module_root/go.mod"
test -d "$module_root/cmd/capt-hookd"
command -v go >/dev/null
command -v lipo >/dev/null

tmp="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp"
}
trap cleanup EXIT

for arch in arm64 amd64; do
  (
    cd "$module_root"
    CGO_ENABLED=0 GOOS=darwin GOARCH="$arch" go build \
      -trimpath \
      -ldflags "-s -w -X github.com/yasyf/captain-hook/internal/hookd.Build=$version" \
      -o "$tmp/capt-hookd-$arch" \
      ./cmd/capt-hookd
  )
done

mkdir -p "$(dirname "$output")"
lipo -create "$tmp/capt-hookd-arm64" "$tmp/capt-hookd-amd64" -output "$output"
chmod 0755 "$output"

archs=" $(lipo -archs "$output") "
[[ "$archs" == *" arm64 "* ]]
[[ "$archs" == *" x86_64 "* ]]
test "$(wc -w <<< "$archs" | tr -d ' ')" = 2
version_json="$("$output" version)"
python3 - "$version" "$version_json" <<'PY'
import json
import sys

expected, raw = sys.argv[1:]
assert json.loads(raw) == {"schema": 1, "build": expected}
PY
