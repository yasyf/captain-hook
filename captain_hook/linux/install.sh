#!/bin/bash
set -euo pipefail

fail() {
  echo "captain-hook install: $1" >&2
  exit 1
}

[ "$(uname -s)-$(uname -m)" = "Linux-x86_64" ] || fail "the Linux host ships for linux amd64 only"
command -v uv >/dev/null || fail "uv must be on PATH to install the capt-hook tool env"

version="${1:-$(sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' "${0%/*}/../.claude-plugin/plugin.json")}"
[ -n "$version" ] || fail "no version given and none in plugin.json"
tag="v$version"
asset="captain-hook-$tag-linux-amd64.tar.gz"
release="https://github.com/yasyf/captain-hook/releases/download/$tag"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fsSL --retry 2 --connect-timeout 10 --max-time 300 -o "$work/$asset" "$release/$asset" \
  || fail "could not download $release/$asset"
curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 -o "$work/SHA256SUMS.txt" "$release/SHA256SUMS.txt" \
  || fail "could not download $release/SHA256SUMS.txt"
awk -v asset="./$asset" '$2 == asset' "$work/SHA256SUMS.txt" | (cd "$work" && sha256sum --check --strict --quiet -) \
  || fail "$asset does not match the release's SHA256SUMS.txt"
tar -xzf "$work/$asset" -C "$work" capt-hookd
"$work/capt-hookd" package-install
