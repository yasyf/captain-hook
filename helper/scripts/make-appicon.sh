#!/usr/bin/env bash

# Regenerate the AppIcon PNGs from docs/assets/logo.png via sips. Committed like
# cc-pool's, so the asset catalog compiles without a build-time image step.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
src="${1:-$here/../docs/assets/logo.png}"
out="$here/Sources/App/Assets.xcassets/AppIcon.appiconset"

if [ ! -f "$src" ]; then
  echo "make-appicon.sh: source logo not found: $src" >&2
  exit 2
fi

mkdir -p "$out"
for size in 16 32 64 128 256 512 1024; do
  sips -s format png -z "$size" "$size" "$src" --out "$out/icon_${size}.png" >/dev/null
done

echo "wrote $out/icon_{16,32,64,128,256,512,1024}.png"
