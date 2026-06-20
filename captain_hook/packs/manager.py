"""Pack model, manifest IO, GitHub fetch, and resolution for capt-hook packs.

A *pack* is a named, versioned collection of hooks. Builtin packs ship inside the
``captain_hook`` wheel under ``captain_hook/packs/<name>/``; external packs live in a
GitHub repo carrying a ``capt-hook.toml`` manifest and are fetched as a tarball into a
local cache. A project enables packs by listing them in ``.claude/hooks/packs.toml``.

packs.toml is the source of truth. An entry may pin a ``commit`` (a hard lock used
directly) or carry only a ``source`` whose ref moves: ``@latest`` (the latest GitHub
release), a branch, or the bare default branch. A moving ref re-resolves to a commit at
most once per 24h, tracked alongside the resolved commit in a per-machine ``PackMeta``
sidecar; within the window the cached commit is used with no network. A declared pack
missing from the cache is fetched on demand, so the first event after a clone or a moved
ref self-heals. The only loud failure is a pack that is both uncached and unreachable.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import re
import shutil
import tarfile
import time
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from filelock import FileLock

from captain_hook import state
from captain_hook.util import http

PACKS_TOML = "packs.toml"
PACK_MANIFEST = "capt-hook.toml"
# A pack's manifest may sit in .claude/ (preferred) or at the repo root.
MANIFEST_MEMBERS = (f".claude/{PACK_MANIFEST}", PACK_MANIFEST)
SHA_MARKER = ".sha"
META_SUFFIX = ".meta"
FASTPATH_NAME = ".resolve-fastpath"
LATEST_REF = "latest"
# Moving refs (@latest / a branch / a bare default-branch source) re-resolve to a fresh
# commit at most once per this window; within it the cached commit is used with no network.
REFRESH_TTL_SECONDS = 24 * 60 * 60
SOURCE_RE = re.compile(r"^github:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:@(?P<ref>[\w./-]+))?$")
PACK_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")
GITHUB_REPO = "https://api.github.com/repos/{owner}/{repo}"
GITHUB_COMMIT = "https://api.github.com/repos/{owner}/{repo}/commits/{ref}"
GITHUB_LATEST_RELEASE = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
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
    # A pinned commit is a hard lock used directly; None means the source's ref
    # (@latest, a branch, or the bare default branch) moves, and the resolved
    # commit lives in a per-machine sidecar (see PackMeta), not in packs.toml.
    commit: str | None = None


type PackEntry = BuiltinPack | ExternalPack


@dataclass(frozen=True, slots=True)
class ResolvedPack:
    entry: PackEntry
    path: Path
    manifest: PackManifest


@dataclass(frozen=True, slots=True)
class PackMeta:
    """Per-machine resolution sidecar for a moving-ref pack: the last-resolved commit and when.

    Stored as JSON next to the cache so the resolved ``commit`` and ``checked_at``
    timestamp never enter the committed ``packs.toml``. ``checked_at`` gates the
    24h re-resolution TTL.
    """

    commit: str
    checked_at: float

    def fresh(self, now: float) -> bool:
        return now - self.checked_at < REFRESH_TTL_SECONDS

    @classmethod
    def load(cls, path: Path) -> PackMeta | None:
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
        return cls(commit=data["commit"], checked_at=data["checked_at"])

    def write(self, path: Path) -> None:
        atomic_write(path, json.dumps({"commit": self.commit, "checked_at": self.checked_at}))


def packs_toml_path(root: Path) -> Path:
    return root / ".claude" / "hooks" / PACKS_TOML


def manifest_in(root: Path) -> Path:
    """Return the pack manifest path under root, preferring .claude/capt-hook.toml.

    Falls back to the repo-root location. The returned path may not exist (the
    canonical missing location), so PackManifest.load still fails loudly.
    """
    claude = root / ".claude" / PACK_MANIFEST
    return claude if claude.is_file() else root / PACK_MANIFEST


def parse_entry(name: str, table: dict[str, Any]) -> PackEntry:
    match table:
        case {"source": source, "commit": commit}:
            return ExternalPack(name=name, source=PackSource.parse(source), commit=commit)
        case {"source": source}:
            return ExternalPack(name=name, source=PackSource.parse(source), commit=None)
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
        case ExternalPack(name=name, source=source, commit=None):
            return f'[packs.{name}]\nsource = "{source}"\n\n'
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


def meta_path(name: str) -> Path:
    return packs_cache_root() / f"{name}{META_SUFFIX}"


def resolve_ref(source: PackSource) -> str:
    """Resolve a source's effective git ref: @latest via the latest release, else the ref or default branch."""
    match source.ref:
        case None:
            return http.github_get_json(GITHUB_REPO.format(owner=source.owner, repo=source.repo))["default_branch"]
        case ref if ref == LATEST_REF:
            return http.github_get_json(GITHUB_LATEST_RELEASE.format(owner=source.owner, repo=source.repo))["tag_name"]
        case ref:
            return ref


def resolve_commit(source: PackSource) -> str:
    return http.github_get_json(GITHUB_COMMIT.format(owner=source.owner, repo=source.repo, ref=resolve_ref(source)))[
        "sha"
    ]


def strip_top_level(tf: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    for member in tf.getmembers():
        if tail := member.name.partition("/")[2]:
            member.path = tail
            yield member


def members_under(members: list[tarfile.TarInfo], hooks: str, manifest_path: str) -> Iterator[tarfile.TarInfo]:
    """Yield the manifest plus members within the pack's hooks dir.

    hooks == "." (hooks beside the manifest) selects the whole tree; a real
    subdir selects only the manifest and that subtree, so the cache holds just
    what the loader imports. The manifest is included by its actual archive path
    so a .claude/ manifest survives without dragging in the rest of .claude/.
    """
    rel = hooks.strip("/")
    prefix = "" if rel in ("", ".") else rel + "/"
    for m in members:
        if m.path == manifest_path or not prefix or m.path == rel or m.path.startswith(prefix):
            yield m


def find_cached(name: str, sha: str) -> Path | None:
    return d if (d := packs_cache_root() / f"{name}@{sha}").is_dir() and (d / SHA_MARKER).is_file() else None


def fetch_commit(source: PackSource, sha: str) -> ResolvedPack:
    root = packs_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / f"{sha}.lock")):
        tarball = root / f".tarball-{sha}.tar.gz"
        http.github_download(GITHUB_TARBALL.format(owner=source.owner, repo=source.repo, sha=sha), tarball)
        staging = root / f".staging-{sha}"
        if staging.exists():
            shutil.rmtree(staging)
        with tarfile.open(tarball) as tf:
            members = list(strip_top_level(tf))
            by_path = {m.path: m for m in members}
            manifest_member = next((by_path[p] for p in MANIFEST_MEMBERS if p in by_path), None)
            if manifest_member is None:
                raise PackError(f"pack manifest {PACK_MANIFEST} missing in {source}")
            tf.extract(manifest_member, staging, filter="data")
            manifest = PackManifest.load(staging / manifest_member.path)
            tf.extractall(
                staging, members=list(members_under(members, manifest.hooks, manifest_member.path)), filter="data"
            )
        tarball.unlink()
        final = root / f"{manifest.name}@{sha}"
        if final.exists():
            shutil.rmtree(final)
        os.replace(staging, final)
        (final / SHA_MARKER).write_text(sha)
    return ResolvedPack(
        ExternalPack(name=manifest.name, source=source, commit=sha), manifest.hooks_dir(final), manifest
    )


def fetch_pack(source: PackSource) -> ResolvedPack:
    try:
        return fetch_commit(source, resolve_commit(source))
    except http.GitHubFetchError as e:
        raise PackError(str(e)) from e


def add_external(source: PackSource) -> ExternalPack:
    """Fetch a source to validate and warm the cache, then return its packs.toml entry.

    A concrete ref (a tag or branch) is FROZEN: the entry carries the resolved commit,
    so packs.toml is a reproducible lockfile and runtime uses the pin directly. A moving
    ref — ``@latest`` or a bare source (default branch) — stays source-only (``commit=None``)
    and the 24h TTL keeps the resolved commit in the per-machine sidecar instead.
    """
    fetched = fetch_pack(source)
    commit = fetched_commit(fetched)
    if source.ref is None or source.ref == LATEST_REF:
        PackMeta(commit=commit, checked_at=time.time()).write(meta_path(fetched.manifest.name))
        return ExternalPack(name=fetched.manifest.name, source=source, commit=None)
    return ExternalPack(name=fetched.manifest.name, source=source, commit=commit)


def fetched_commit(resolved: ResolvedPack) -> str:
    """The commit a just-fetched external pack resolved to (fetch_commit always pins one)."""
    if not isinstance(resolved.entry, ExternalPack) or resolved.entry.commit is None:
        raise PackError(f"expected a fetched external pack, got {resolved.entry!r}")
    return resolved.entry.commit


def builtin_packs() -> dict[str, Path]:
    base = Path(str(importlib.resources.files("captain_hook") / "packs"))
    return {p.name: p for p in base.iterdir() if p.is_dir() and manifest_in(p).is_file()}


def resolve_builtin(name: str) -> ResolvedPack:
    if not (pack_dir := builtin_packs().get(name)):
        raise PackError(f"unknown builtin pack {name!r}; available: {', '.join(sorted(builtin_packs())) or 'none'}")
    manifest = PackManifest.load(manifest_in(pack_dir))
    return ResolvedPack(BuiltinPack(name=name), manifest.hooks_dir(pack_dir), manifest)


def load_cached(entry: ExternalPack, sha: str) -> ResolvedPack | None:
    if not (cached := find_cached(entry.name, sha)):
        return None
    manifest = PackManifest.load(manifest_in(cached))
    return ResolvedPack(entry, manifest.hooks_dir(cached), manifest)


def resolve_external(entry: ExternalPack) -> ResolvedPack | None:
    """Resolve a declared external pack to its cached content, fetching on a cache miss.

    A pinned ``commit`` is a hard lock used directly. A moving ref re-resolves to a
    fresh commit at most once per 24h (tracked in the PackMeta sidecar); within the
    window the cached commit is used with no network. Returns None only when nothing
    is cached and the network is unreachable — the single loud "not cached" path.
    """
    if entry.commit is not None:
        return load_cached(entry, entry.commit) or auto_fetch(entry, entry.commit)
    return resolve_moving(entry)


def auto_fetch(entry: ExternalPack, sha: str) -> ResolvedPack | None:
    """Fetch a known commit on a cache miss, returning None if the network is unreachable.

    Re-loads through the declared ``entry`` so the resolved pack reports the packs.toml
    identity (name, source, pinned-or-moving commit), not fetch_commit's synthesized entry.
    """
    try:
        fetch_commit(entry.source, sha)
    except http.GitHubFetchError:
        return None
    return load_cached(entry, sha)


def resolve_moving(entry: ExternalPack) -> ResolvedPack | None:
    now = time.time()
    cached_meta = PackMeta.load(meta_path(entry.name))
    if cached_meta and cached_meta.fresh(now) and (hit := load_cached(entry, cached_meta.commit)):
        return hit
    try:
        sha = resolve_commit(entry.source)
    except http.GitHubFetchError:
        return load_cached(entry, cached_meta.commit) if cached_meta else None
    resolved = load_cached(entry, sha) or auto_fetch(entry, sha)
    if resolved is not None:
        PackMeta(commit=sha, checked_at=now).write(meta_path(entry.name))
    return resolved


def cached_commit(entry: ExternalPack, now: float) -> str | None:
    """The commit to load from cache with no network: the pin, or a within-TTL sidecar commit."""
    match entry.commit:
        case None if (meta := PackMeta.load(meta_path(entry.name))) and meta.fresh(now):
            return meta.commit
        case None:
            return None
        case commit:
            return commit


def resolved_commit(entry: ExternalPack) -> str | None:
    """The commit this entry last resolved to for display: its pin, else the sidecar's record (ignoring TTL)."""
    return entry.commit if entry.commit is not None else (m := PackMeta.load(meta_path(entry.name))) and m.commit


def load_cached_fresh(entry: ExternalPack, now: float) -> ResolvedPack:
    """Load a pack the fast path already proved is cached and within TTL; crash if the invariant broke."""
    if (sha := cached_commit(entry, now)) is None or (hit := load_cached(entry, sha)) is None:
        raise PackError(f"fast-path invariant violated: {entry.name} no longer cached")
    return hit


def all_cached_and_fresh(entries: Sequence[PackEntry], now: float) -> bool:
    return all(
        bool((sha := cached_commit(ext, now)) and find_cached(ext.name, sha))
        for ext in entries
        if isinstance(ext, ExternalPack)
    )


def fastpath_path(root: Path) -> Path:
    return packs_cache_root() / f"{sha256(str(root.resolve()).encode()).hexdigest()[:16]}{FASTPATH_NAME}"


def fastpath_unchanged(root: Path, entries: Sequence[PackEntry], now: float) -> bool:
    """Fast skip: packs.toml is byte-identical to the last resolve and every pack is cached within TTL.

    Avoids any re-resolution on the hot path. The recorded hash is a per-machine,
    per-project sidecar; a stale or missing one just forces the full resolve below. No network.
    """
    fastpath = fastpath_path(root)
    return fastpath.is_file() and fastpath.read_text() == toml_hash(root) and all_cached_and_fresh(entries, now)


def toml_hash(root: Path) -> str:
    path = packs_toml_path(root)
    return sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def resolve_enabled_packs(root: Path) -> tuple[list[ResolvedPack], list[str]]:
    entries = read_entries(packs_toml_path(root))
    now = time.time()
    fast = fastpath_unchanged(root, entries, now)
    resolved: list[ResolvedPack] = []
    missing: list[str] = []
    for entry in entries:
        match entry:
            case BuiltinPack(name=name):
                resolved.append(resolve_builtin(name))
            case ExternalPack() as ext if found := (load_cached_fresh(ext, now) if fast else resolve_external(ext)):
                resolved.append(found)
            case ExternalPack() as ext:
                missing.append(ext.name)
    if not (fast or missing):
        atomic_write(fastpath_path(root), toml_hash(root))
    return resolved, missing
