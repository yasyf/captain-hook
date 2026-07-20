from __future__ import annotations

import subprocess
from pathlib import Path

ESBUILD_VERSION = "0.24.2"
SRC_DIR = Path(__file__).resolve().parents[1] / "tutorial" / "_src"
ENTRY = "emulator.ts"
BUNDLE = SRC_DIR.parent / "widgets" / "emulator.js"


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
            "--platform=browser",
            "--log-level=warning",
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
