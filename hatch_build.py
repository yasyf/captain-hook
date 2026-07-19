from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
import urllib.error
import urllib.request
from functools import cache
from importlib.metadata import version as dist_version
from pathlib import Path
from typing import NamedTuple

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

PYPI_JSON = "https://pypi.org/pypi/ast-grep-py/{version}/json"
CRATE_URL = "https://static.crates.io/crates/{name}/{name}-{version}.crate"
USER_AGENT = "capt-hook-build (github.com/yasyf/captain-hook)"
REGEN_COMMAND = "uv run python hatch_build.py"

KEY_OVERRIDES = {"Cpp": "cpp", "Kotlin": "kotlin", "Solidity": "solidity", "Yaml": "yaml"}
EXTRA_PARSER_FNS = {"Html": "language_html"}
NODE_TYPES_SUBDIRS = {"LANGUAGE_TYPESCRIPT": "typescript/", "LANGUAGE_TSX": "tsx/", "LANGUAGE_PHP_ONLY": "php_only/"}
COMMENT_KIND_OVERRIDES: dict[str, dict[str, tuple[str, ...]]] = {}
COMMENTLESS_LANGS = frozenset({"md"})

ALIASES_BLOCK = re.compile(r"impl_aliases!\s*\{(.*?)\n\}", re.DOTALL)
EXTENSIONS_BLOCK = re.compile(r"fn extensions\(.*?\n\}", re.DOTALL)
MATCH_ARM = re.compile(r"(\w+)\s*=>\s*&\[(.*?)\]", re.DOTALL)
QUOTED = re.compile(r'"([^"]*)"')
PARSER_FN = re.compile(r"impl_lang(?:_expando)?!\(\s*(\w+)\s*,\s*(\w+)")
PARSER_CONDITIONAL = re.compile(
    r'pub fn (\w+)\(\)\s*->\s*TSLanguage\s*\{\s*conditional_lang!\(\s*\w+\s*,\s*"([^"]+)"\s*(?:,\s*(\w+)\s*)?\)',
    re.DOTALL,
)
DEP_LINE = re.compile(r"^\s*(tree-sitter-[\w-]+)\s*=\s*\{([^}]*)\}", re.MULTILINE)
DEP_PACKAGE = re.compile(r'package\s*=\s*"([^"]+)"')
PACKAGE_BLOCK = re.compile(
    r'\[\[package\]\]\s*\nname = "([^"]+)"\nversion = "([^"]+)"(?:\nsource = "[^"]+")?\nchecksum = "([0-9a-f]+)"'
)


class Sources(NamedTuple):
    lock: str
    cargo_toml: str
    lib: str
    parsers: str


class Crate(NamedTuple):
    package: str
    version: str
    sha256: str
    subdir: str


def ast_grep_version() -> str:
    return dist_version("ast-grep-py")


def cache_dir() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "capt-hook-build" / f"ast-grep-py-{ast_grep_version()}"


def fetch(url: str, *, name: str, sha256: str | None = None) -> bytes:
    path = cache_dir() / name
    if path.exists() and (
        (data := path.read_bytes()) and (sha256 is None or hashlib.sha256(data).hexdigest() == sha256)
    ):
        return data
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request) as response:
            data = response.read()
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"cannot fetch {url}: {error}; warm the build cache at {cache_dir()} with `uv build` while online"
        ) from error
    if sha256 is not None and (actual := hashlib.sha256(data).hexdigest()) != sha256:
        raise RuntimeError(f"checksum mismatch for {url}: expected {sha256}, got {actual}")
    cache_dir().mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=cache_dir(), delete=False) as tmp:
        tmp.write(data)
    os.replace(tmp.name, path)
    return data


@cache
def sdist_source() -> tuple[str, str]:
    payload = json.loads(fetch(PYPI_JSON.format(version=ast_grep_version()), name=f"pypi-{ast_grep_version()}.json"))
    for entry in payload["urls"]:
        if entry["packagetype"] == "sdist":
            return entry["url"], entry["digests"]["sha256"]
    raise RuntimeError(f"no sdist in PyPI metadata for ast-grep-py {ast_grep_version()}")


@cache
def sdist_sources() -> Sources:
    url, sha256 = sdist_source()
    data = fetch(url, name=f"ast_grep_py-{ast_grep_version()}.tar.gz", sha256=sha256)
    root = f"ast_grep_py-{ast_grep_version()}"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:

        def member(name: str) -> str:
            if (handle := archive.extractfile(f"{root}/{name}")) is None:
                raise RuntimeError(f"{name} missing from ast-grep-py sdist")
            return handle.read().decode()

        return Sources(
            lock=member("Cargo.lock"),
            cargo_toml=member("crates/language/Cargo.toml"),
            lib=member("crates/language/src/lib.rs"),
            parsers=member("crates/language/src/parsers.rs"),
        )


def cargo_checksums(lock: str) -> dict[str, tuple[str, str]]:
    return {m.group(1): (m.group(2), m.group(3)) for m in PACKAGE_BLOCK.finditer(lock)}


def parse_aliases(lib: str) -> dict[str, tuple[str, ...]]:
    if (block := ALIASES_BLOCK.search(lib)) is None:
        raise RuntimeError(f"no `impl_aliases!` block in ast-grep-py {ast_grep_version()} lib.rs")
    return {m.group(1): tuple(QUOTED.findall(m.group(2))) for m in MATCH_ARM.finditer(block.group(1))}


def parse_extensions(lib: str) -> dict[str, tuple[str, ...]]:
    if (block := EXTENSIONS_BLOCK.search(lib)) is None:
        raise RuntimeError(f"no `fn extensions` block in ast-grep-py {ast_grep_version()} lib.rs")
    return {m.group(1): tuple(QUOTED.findall(m.group(2))) for m in MATCH_ARM.finditer(block.group(0))}


def parser_fns(lib: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in PARSER_FN.finditer(lib)} | EXTRA_PARSER_FNS


def parser_table(parsers: str) -> dict[str, tuple[str, str | None]]:
    return {m.group(1): (m.group(2), m.group(3)) for m in PARSER_CONDITIONAL.finditer(parsers)}


def dep_packages(cargo_toml: str) -> dict[str, str]:
    return {
        m.group(1): (pkg.group(1) if (pkg := DEP_PACKAGE.search(m.group(2))) else m.group(1))
        for m in DEP_LINE.finditer(cargo_toml)
    }


def parser_crates(parsers: str, lib: str, cargo_toml: str, checksums: dict[str, tuple[str, str]]) -> dict[str, Crate]:
    table = parser_table(parsers)
    packages = dep_packages(cargo_toml)
    crates = {}
    for variant, func in parser_fns(lib).items():
        dep_key, field = table[func]
        version, sha256 = checksums[packages[dep_key]]
        crates[variant] = Crate(packages[dep_key], version, sha256, "" if field is None else NODE_TYPES_SUBDIRS[field])
    return crates


def lang_keys(aliases: dict[str, tuple[str, ...]]) -> dict[str, str]:
    keys = {}
    for variant, variant_aliases in aliases.items():
        if (key := KEY_OVERRIDES.get(variant) or min(variant_aliases, key=len)) not in variant_aliases:
            raise RuntimeError(f"lang key {key!r} for {variant} is not an upstream alias of {variant_aliases}")
        keys[variant] = key
    return keys


def comment_kinds(nodes: list[dict[str, object]]) -> set[str]:
    by_type = {node["type"]: node for node in nodes}
    named = {kind for kind, node in by_type.items() if node.get("named") and "comment" in kind}
    referenced = {
        child["type"]
        for kind in named
        for group in (*by_type[kind].get("fields", {}).values(), by_type[kind].get("children", {}))
        for child in group.get("types", [])
    }
    return named - referenced


@cache
def variant_keys() -> dict[str, str]:
    return lang_keys(parse_aliases(sdist_sources().lib))


@cache
def crate_table() -> dict[str, Crate]:
    sources = sdist_sources()
    crates = parser_crates(sources.parsers, sources.lib, sources.cargo_toml, cargo_checksums(sources.lock))
    if missing := sorted(set(parse_aliases(sources.lib)) - set(crates)):
        raise RuntimeError(f"no parser crate resolved for languages: {missing}")
    return crates


def grammar_node_types(crate: Crate) -> list[dict[str, object]]:
    data = fetch(
        CRATE_URL.format(name=crate.package, version=crate.version),
        name=f"{crate.package}-{crate.version}.crate",
        sha256=crate.sha256,
    )
    member = f"{crate.package}-{crate.version}/{crate.subdir}src/node-types.json"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        if (handle := archive.extractfile(member)) is None:
            raise RuntimeError(f"{member} missing from {crate.package} {crate.version}")
        return json.load(handle)


def build_lang_globs(lib: str) -> dict[str, tuple[str, ...]]:
    extensions = parse_extensions(lib)
    keys = lang_keys(parse_aliases(lib))
    if missing := sorted(set(keys) - set(extensions)):
        raise RuntimeError(f"no extensions parsed for languages: {missing!r}")
    owners: dict[str, str] = {}
    for variant, variant_exts in extensions.items():
        for ext in variant_exts:
            if ext in owners:
                raise RuntimeError(f"extension {ext!r} claimed by both {owners[ext]} and {variant}")
            owners[ext] = variant
    return {
        keys[variant]: tuple(sorted(f"*.{ext}" for ext in variant_exts))
        for variant, variant_exts in sorted(extensions.items(), key=lambda item: keys[item[0]])
    }


def build_comment_types() -> frozenset[str]:
    keys = variant_keys()
    by_lang: dict[str, frozenset[str]] = {}
    for variant, crate in crate_table().items():
        if (key := keys[variant]) in COMMENTLESS_LANGS:
            continue
        override = COMMENT_KIND_OVERRIDES.get(key, {})
        kinds = comment_kinds(grammar_node_types(crate)) | set(override.get("add", ()))
        by_lang[key] = frozenset(kinds - set(override.get("remove", ())))
    if empty := sorted(lang for lang, kinds in by_lang.items() if not kinds):
        raise RuntimeError(f"languages define no comment kinds: {empty}")
    return frozenset(kind for kinds in by_lang.values() for kind in kinds)


def render_langs() -> str:
    globs = build_lang_globs(sdist_sources().lib)
    globs_body = "\n".join(f"    {key!r}: {value!r}," for key, value in globs.items())
    comments_body = "\n".join(f"        {kind!r}," for kind in sorted(build_comment_types()))
    return (
        f"# GENERATED by `{REGEN_COMMAND}`.\n"
        f"# ast-grep-py {ast_grep_version()}.\n\n"
        "from __future__ import annotations\n\n"
        "LANG_GLOBS: dict[str, tuple[str, ...]] = {\n"
        f"{globs_body}\n"
        "}\n\n"
        "COMMENT_TYPES: frozenset[str] = frozenset(\n"
        "    {\n"
        f"{comments_body}\n"
        "    }\n"
        ")\n"
    )


def write_langs(path: Path) -> None:
    path.write_text(render_langs())


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        write_langs(Path(self.root) / "captain_hook" / "langs.py")


if __name__ == "__main__":
    write_langs(Path(__file__).parent / "captain_hook" / "langs.py")
