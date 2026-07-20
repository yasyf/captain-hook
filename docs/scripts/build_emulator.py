from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

ESBUILD_VERSION = "0.24.2"
SRC_DIR = Path(__file__).resolve().parents[1] / "tutorial" / "_src"
ENTRY = "emulator.ts"
BUNDLE = SRC_DIR.parent / "widgets" / "emulator.js"
BANNER_PREFIX = "// capt-hook-widget src-sha256: "


def src_hash() -> str:
    digest = hashlib.sha256(ESBUILD_VERSION.encode())
    for path in sorted(SRC_DIR.glob("*.ts")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build(out: Path = BUNDLE) -> Path:
    """Bundle the emulator TS to a deterministic, non-minified ESM file via pinned esbuild."""
    subprocess.run(
        [
            "npx",
            "-y",
            f"esbuild@{ESBUILD_VERSION}",
            ENTRY,
            "--bundle",
            "--format=esm",
            "--target=es2022",
            "--platform=browser",
            "--log-level=warning",
            f"--banner:js={BANNER_PREFIX}{src_hash()}",
            f"--outfile={out.resolve()}",
        ],
        cwd=SRC_DIR,
        check=True,
    )
    return out


def main() -> None:
    build()


if __name__ == "__main__":
    main()
