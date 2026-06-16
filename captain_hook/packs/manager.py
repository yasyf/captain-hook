"""Pack model, manifest IO, GitHub fetch, and resolution for capt-hook packs.

A *pack* is a named, versioned collection of hooks. Builtin packs ship inside the
``captain_hook`` wheel under ``captain_hook/packs/<name>/``; external packs live in a
GitHub repo carrying a ``capt-hook.toml`` manifest and are fetched as a pinned tarball
into a local cache. A project enables packs by listing them in ``.claude/hooks/packs.toml``.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import re
import shutil
import tarfile
import tomllib
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock

from captain_hook import state

PACKS_TOML = "packs.toml"
PACK_MANIFEST = "capt-hook.toml"
SHA_MARKER = ".sha"
SOURCE_RE = re.compile(r"^github:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:@(?P<ref>[\w./-]+))?$")
PACK_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")
GITHUB_REPO = "https://api.github.com/repos/{owner}/{repo}"
GITHUB_COMMIT = "https://api.github.com/repos/{owner}/{repo}/commits/{ref}"
GITHUB_TARBALL = "https://github.com/{owner}/{repo}/archive/{sha}.tar.gz"


class PackError(Exception):
    """A pack source, manifest, or enabled-packs entry was invalid or unresolvable."""


@dataclass(frozen=True, slots=True)
class PackSource:
    owner: str
    repo: str
    ref: str | None

    @classmethod
    def parse(cls, raw: str) -> PackSource:
        if not (m := SOURCE_RE.match(raw)):
            raise PackError(f"invalid pack source {raw!r}; expected github:owner/repo[@ref]")
        return cls(owner=m["owner"], repo=m["repo"], ref=m["ref"])

    def __str__(self) -> str:
        return f"github:{self.owner}/{self.repo}" + (f"@{self.ref}" if self.ref else "")


@dataclass(frozen=True, slots=True)
class PackManifest:
    name: str
    version: str
    description: str
    hooks: str

    @classmethod
    def load(cls, path: Path) -> PackManifest:
        if not path.is_file():
            raise PackError(f"pack manifest {PACK_MANIFEST} missing at {path.parent}")
        data = tomllib.loads(path.read_text())
        manifest = cls(name=data["name"], version=data["version"], description=data["description"], hooks=data["hooks"])
        if not PACK_NAME_RE.fullmatch(manifest.name):
            raise PackError(f"pack name {manifest.name!r} must match {PACK_NAME_RE.pattern}")
        return manifest

    def hooks_dir(self, root: Path) -> Path:
        return root / self.hooks


@dataclass(frozen=True, slots=True)
class BuiltinPack:
    name: str


@dataclass(frozen=True, slots=True)
class ExternalPack:
    name: str
    source: PackSource
    commit: str


type PackEntry = BuiltinPack | ExternalPack


@dataclass(frozen=True, slots=True)
class ResolvedPack:
    entry: PackEntry
    path: Path
    manifest: PackManifest


def packs_toml_path(root: Path) -> Path:
    return root / ".claude" / "hooks" / PACKS_TOML


def parse_entry(name: str, table: dict[str, Any]) -> PackEntry:
    match table:
        case {"source": source, "commit": commit}:
            return ExternalPack(name=name, source=PackSource.parse(source), commit=commit)
        case {} if not table:
            return BuiltinPack(name=name)
        case _:
            raise PackError(f"invalid pack entry [packs.{name}]: {table!r}")


def read_entries(path: Path) -> list[PackEntry]:
    if not path.exists():
        return []
    return [parse_entry(name, table) for name, table in (tomllib.loads(path.read_text()).get("packs") or {}).items()]


def render_entry(entry: PackEntry) -> str:
    match entry:
        case BuiltinPack(name=name):
            return f"[packs.{name}]\n\n"
        case ExternalPack(name=name, source=source, commit=commit):
            return f'[packs.{name}]\nsource = "{source}"\ncommit = "{commit}"\n\n'


def render_packs_toml(entries: Sequence[PackEntry]) -> str:
    return "".join(render_entry(e) for e in sorted(entries, key=lambda e: e.name))


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def upsert_entry(path: Path, entry: PackEntry) -> None:
    atomic_write(path, render_packs_toml([*(e for e in read_entries(path) if e.name != entry.name), entry]))


def delete_entry(path: Path, name: str) -> None:
    entries = read_entries(path)
    if name not in {e.name for e in entries}:
        raise PackError(f"pack {name!r} is not enabled in {path}")
    atomic_write(path, render_packs_toml([e for e in entries if e.name != name]))


def packs_cache_root() -> Path:
    return state.CACHE_ROOT / "packs"


def github_headers() -> dict[str, str]:
    return {"Accept": "application/vnd.github+json", "User-Agent": "capt-hook"} | (
        {"Authorization": f"Bearer {token}"} if (token := os.environ.get("GITHUB_TOKEN")) else {}
    )


def github_get(url: str) -> Any:
    with urllib.request.urlopen(urllib.request.Request(url, headers=github_headers())) as resp:
        return json.load(resp)


def resolve_commit(source: PackSource) -> str:
    ref = source.ref or github_get(GITHUB_REPO.format(owner=source.owner, repo=source.repo))["default_branch"]
    return github_get(GITHUB_COMMIT.format(owner=source.owner, repo=source.repo, ref=ref))["sha"]


def strip_top_level(tf: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    for member in tf.getmembers():
        if (tail := member.name.partition("/")[2]):
            member.path = tail
            yield member


def find_cached(name: str, sha: str) -> Path | None:
    return d if (d := packs_cache_root() / f"{name}@{sha}").is_dir() and (d / SHA_MARKER).is_file() else None


def fetch_commit(source: PackSource, sha: str) -> ResolvedPack:
    root = packs_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / f"{sha}.lock")):
        tarball, _ = urllib.request.urlretrieve(GITHUB_TARBALL.format(owner=source.owner, repo=source.repo, sha=sha))
        staging = root / f".staging-{sha}"
        if staging.exists():
            shutil.rmtree(staging)
        with tarfile.open(tarball) as tf:
            tf.extractall(staging, members=strip_top_level(tf), filter="data")
        manifest = PackManifest.load(staging / PACK_MANIFEST)
        final = root / f"{manifest.name}@{sha}"
        if final.exists():
            shutil.rmtree(final)
        os.replace(staging, final)
        (final / SHA_MARKER).write_text(sha)
    return ResolvedPack(ExternalPack(name=manifest.name, source=source, commit=sha), manifest.hooks_dir(final), manifest)


def fetch_pack(source: PackSource) -> ResolvedPack:
    return fetch_commit(source, resolve_commit(source))


def builtin_packs() -> dict[str, Path]:
    base = Path(str(importlib.resources.files("captain_hook") / "packs"))
    return {p.name: p for p in base.iterdir() if p.is_dir() and (p / PACK_MANIFEST).is_file()}


def resolve_builtin(name: str) -> ResolvedPack:
    if not (pack_dir := builtin_packs().get(name)):
        raise PackError(f"unknown builtin pack {name!r}; available: {', '.join(sorted(builtin_packs())) or 'none'}")
    manifest = PackManifest.load(pack_dir / PACK_MANIFEST)
    return ResolvedPack(BuiltinPack(name=name), manifest.hooks_dir(pack_dir), manifest)


def resolve_external(entry: ExternalPack) -> ResolvedPack | None:
    if not (cached := find_cached(entry.name, entry.commit)):
        return None
    manifest = PackManifest.load(cached / PACK_MANIFEST)
    return ResolvedPack(entry, manifest.hooks_dir(cached), manifest)


def resolve_enabled_packs(root: Path) -> tuple[list[ResolvedPack], list[str]]:
    resolved: list[ResolvedPack] = []
    missing: list[str] = []
    for entry in read_entries(packs_toml_path(root)):
        match entry:
            case BuiltinPack(name=name):
                resolved.append(resolve_builtin(name))
            case ExternalPack() as ext:
                resolved.append(found) if (found := resolve_external(ext)) else missing.append(ext.name)
    return resolved, missing
