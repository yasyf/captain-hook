#!/usr/bin/env bash

# Write a per-build Version.xcconfig; run BEFORE `xcodegen generate`.

# Usage: gen-version-xcconfig.sh <marketing-version> [out.xcconfig]
set -euo pipefail

marketing_version="${1:-${MARKETING_VERSION:-}}"
out="${2:-${VERSION_XCCONFIG_PATH:-Version.xcconfig}}"

if [ -z "$marketing_version" ]; then
  echo "gen-version-xcconfig.sh: marketing version required (arg 1 or \$MARKETING_VERSION)" >&2
  exit 2
fi

# epoch-nanos: unique + monotonic per build.
build_number="$(python3 -c 'import time; print(time.time_ns())')"

{
  printf '// GENERATED per build by gen-version-xcconfig.sh — do not commit.\n'
  printf 'MARKETING_VERSION = %s\n' "$marketing_version"
  printf 'CURRENT_PROJECT_VERSION = %s\n' "$build_number"
} >"$out"

echo "wrote $out (MARKETING_VERSION=$marketing_version CURRENT_PROJECT_VERSION=$build_number)" >&2
