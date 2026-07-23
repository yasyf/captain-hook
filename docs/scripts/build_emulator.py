from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ESBUILD_VERSION = "0.24.2"
SRC_DIR = Path(__file__).resolve().parents[1] / "tutorial" / "_src"
WIDGETS_DIR = SRC_DIR.parent / "widgets"
NODE_MODULES = SRC_DIR / "node_modules"
TOOL_ALIASES_JSON = SRC_DIR / "generated" / "tool_aliases.json"
LOCK = SRC_DIR / "package-lock.json"
CI_STAMP = NODE_MODULES / ".capt-hook-ci-stamp"

# The wllama single-thread WebAssembly runtime is the one asset esbuild can't bundle (a binary
# loaded by URL at runtime), so it is copied out of node_modules and self-hosted alongside the
# bundles; llm.js passes its committed URL through as the wllama lane's `assets.default`.
WLLAMA_WASM_SRC = NODE_MODULES / "@wllama" / "wllama" / "esm" / "wasm" / "wllama.wasm"
WLLAMA_WASM_DST = WIDGETS_DIR / "wllama" / "wllama.wasm"

BANNER_PREFIX = "// capt-hook-widget src-sha256: "
COMMON_FLAGS = ("--bundle", "--format=esm", "--target=es2022", "--platform=browser", "--log-level=warning")

# Config and generated inputs (beyond the TS sources) that every bundle's hash covers.
HASHED_INPUTS = ("package.json", "package-lock.json", "tsconfig.json", "generated/tool_aliases.json")


@dataclass(frozen=True, slots=True)
class Bundle:
    entry: str
    outfile: Path
    flags: tuple[str, ...]


BUNDLES = (
    Bundle("emulator.ts", WIDGETS_DIR / "emulator.js", ()),
    Bundle("compiler/index.ts", WIDGETS_DIR / "compiler.js", ("--minify",)),
    Bundle("editor.ts", WIDGETS_DIR / "editor.js", ("--minify",)),
    Bundle("llm.ts", WIDGETS_DIR / "llm.js", ("--minify",)),
)


def tool_alias_map() -> dict[str, list[str]]:
    """The full tool-alias expansion serialize_condition relies on, via the native map's closure."""
    from cc_transcript.tools import TOOL_ALIASES, expand_tool_names

    reached: set[str] = set()
    frontier = list(set(TOOL_ALIASES) | set(TOOL_ALIASES.values()))
    while frontier:
        if (name := frontier.pop()) in reached:
            continue
        reached.add(name)
        frontier.extend(alias for alias in expand_tool_names(name) if alias not in reached)
    return {name: expansion for name in sorted(reached) if (expansion := sorted(expand_tool_names(name))) != [name]}


def write_tool_aliases() -> None:
    TOOL_ALIASES_JSON.parent.mkdir(parents=True, exist_ok=True)
    TOOL_ALIASES_JSON.write_text(json.dumps(tool_alias_map(), indent=2, sort_keys=True) + "\n")


def ensure_node_modules() -> None:
    want = hashlib.sha256(LOCK.read_bytes()).hexdigest()
    if CI_STAMP.exists() and CI_STAMP.read_text() == want:
        return
    subprocess.run(["npm", "ci"], cwd=SRC_DIR, check=True)
    CI_STAMP.write_text(want)


def copy_wllama_wasm() -> None:
    WLLAMA_WASM_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(WLLAMA_WASM_SRC, WLLAMA_WASM_DST)


def typecheck() -> None:
    subprocess.run([str(NODE_MODULES / ".bin" / "tsc"), "--noEmit"], cwd=SRC_DIR, check=True)


def src_hash() -> str:
    digest = hashlib.sha256(ESBUILD_VERSION.encode())
    digest.update("\0".join(COMMON_FLAGS).encode())
    digest.update(b"\0")
    for bundle in BUNDLES:
        for part in (bundle.entry, bundle.outfile.name, "\0".join(bundle.flags)):
            digest.update(part.encode())
            digest.update(b"\0")
    for path in sorted(SRC_DIR.rglob("*.ts")):
        if "node_modules" in path.parts:
            continue
        digest.update(path.relative_to(SRC_DIR).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    for name in HASHED_INPUTS:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update((SRC_DIR / name).read_bytes())
        digest.update(b"\0")
    digest.update(WLLAMA_WASM_DST.name.encode())
    digest.update(b"\0")
    digest.update(WLLAMA_WASM_DST.read_bytes())
    digest.update(b"\0")
    return digest.hexdigest()


def esbuild(bundle: Bundle, banner: str) -> None:
    subprocess.run(
        [
            str(NODE_MODULES / ".bin" / "esbuild"),
            bundle.entry,
            *COMMON_FLAGS,
            *bundle.flags,
            f"--banner:js={BANNER_PREFIX}{banner}",
            f"--outfile={bundle.outfile}",
        ],
        cwd=SRC_DIR,
        check=True,
    )


def build() -> None:
    """Regenerate aliases, install/typecheck, self-host the wasm, then bundle every target."""
    ensure_node_modules()
    write_tool_aliases()
    copy_wllama_wasm()
    typecheck()
    banner = src_hash()
    for bundle in BUNDLES:
        esbuild(bundle, banner)


def main() -> None:
    build()


if __name__ == "__main__":
    main()
