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

import contextlib
import importlib.resources
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from filelock import FileLock
from loguru import logger

from captain_hook.util import http
from captain_hook.util.paths import resolve_cache_dir

PACK_MANIFEST = "capt-hook.toml"
ATTACHED_FILE = "attached_packs.json"
SHA_MARKER = ".sha"
LATEST_REF = "latest"
# Moving refs (@latest / a branch / a bare default-branch source) re-resolve to a fresh
# commit at most once per this window; within it the cached commit is used with no network.
REFRESH_TTL_SECONDS = 24 * 60 * 60
PACK_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")
# Cached commit dirs kept per pack besides the just-resolved one: a recency buffer
# so a rollback or a still-loading prior session's pin survives one fresh fetch.
KEEP_COMMITS = 2


def pack_module_name(name: str) -> str:
    """The import-safe module component a pack loads under (a pack name's hyphens become underscores)."""
    return re.sub(r"\W", "_", name)


class PackError(Exception):
    """A pack source, manifest, or enabled-packs entry was invalid or unresolvable."""


@dataclass(frozen=True, slots=True)
class PackSource:
    owner: str
    repo: str
    ref: str | None

    @classmethod
    def parse(cls, raw: str) -> PackSource:
        if not (m := re.match(r"^github:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:@(?P<ref>[\w./-]+))?$", raw)):
            raise PackError(f"invalid pack source {raw!r}; expected github:owner/repo[@ref]")
        return cls(owner=m["owner"], repo=m["repo"], ref=m["ref"])

    def __str__(self) -> str:
        return f"github:{self.owner}/{self.repo}" + (f"@{self.ref}" if self.ref else "")


@dataclass(frozen=True, slots=True)
class PackManifest:
    name: str
    description: str
    hooks: str
    version: str = "0.0.0"
    nlp: bool = False

    @classmethod
    def load(cls, path: Path) -> PackManifest:
        if not path.is_file():
            raise PackError(f"pack manifest {PACK_MANIFEST} missing at {path.parent}")
        data = tomllib.loads(path.read_text())
        manifest = cls(
            name=data["name"],
            description=data["description"],
            hooks=data["hooks"],
            # .get is deliberate: `version` is optional since 9.7 (authors keep the key while
            # pre-9.7 capt-hook is in the wild), and `nlp` is a schema addition manifests predate.
            version=data.get("version", "0.0.0"),
            nlp=data.get("nlp", False),
        )
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


@dataclass(frozen=True, slots=True)
class DisabledPack:
    """A packs.toml entry that declines a pack by name.

    ``[packs.<name>] disabled = true`` suppresses a pack the repo would otherwise
    inherit — a builtin, a packs.toml source, or a plugin-attached pack — regardless of
    any other keys on the entry. Disabling always wins.
    """

    name: str


@dataclass(frozen=True, slots=True)
class AttachedPack:
    """A pack a Claude plugin registered for the current session via ``pack attach``.

    Stored per session in ``attached_packs.json`` keyed by ``name``; ``dir`` is the
    absolute pack root (holding the ``capt-hook.toml`` manifest) and ``version`` is the
    manifest version recorded at attach time.
    """

    name: str
    dir: str
    version: str


type PackEntry = BuiltinPack | ExternalPack | DisabledPack
type ResolvedEntry = BuiltinPack | ExternalPack | AttachedPack


@dataclass(frozen=True, slots=True)
class ResolvedPack:
    entry: ResolvedEntry
    path: Path
    manifest: PackManifest


@dataclass(frozen=True, slots=True)
class PackMeta:
    """Per-machine resolution sidecar for a moving-ref pack: the last-resolved commit, ref, and when.

    Stored as JSON next to the cache so the resolved ``commit``, ``resolved_ref``, and
    ``checked_at`` timestamp never enter the committed ``packs.toml``. ``checked_at`` gates
    the 24h re-resolution TTL; ``resolved_ref`` (the moving ref that resolved — a release tag
    or a branch) is display-only for ``pack list`` and is absent on pre-9.7 sidecars.
    """

    commit: str
    checked_at: float
    resolved_ref: str | None = None

    def fresh(self, now: float) -> bool:
        return now - self.checked_at < REFRESH_TTL_SECONDS

    @classmethod
    def load(cls, path: Path) -> PackMeta | None:
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
        # .get is deliberate: pre-9.7 sidecars lack resolved_ref (mirrors PackManifest.load).
        return cls(commit=data["commit"], checked_at=data["checked_at"], resolved_ref=data.get("resolved_ref"))

    def write(self, path: Path) -> None:
        atomic_write(
            path, json.dumps({"commit": self.commit, "checked_at": self.checked_at, "resolved_ref": self.resolved_ref})
        )


def packs_toml_path(root: Path) -> Path:
    return root / ".claude" / "hooks" / "packs.toml"


def manifest_in(root: Path) -> Path:
    """Return the pack manifest path under root, preferring .claude/capt-hook.toml.

    Falls back to the repo-root location. The returned path may not exist (the
    canonical missing location), so PackManifest.load still fails loudly.
    """
    claude = root / ".claude" / PACK_MANIFEST
    return claude if claude.is_file() else root / PACK_MANIFEST


def parse_entry(name: str, table: dict[str, Any]) -> PackEntry:
    match table:
        case {"disabled": True}:
            return DisabledPack(name=name)
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
        case DisabledPack(name=name):
            return f"[packs.{name}]\ndisabled = true\n\n"
        case ExternalPack(name=name, source=source, commit=None):
            return f'[packs.{name}]\nsource = "{source}"\n\n'
        case ExternalPack(name=name, source=source, commit=commit):
            return f'[packs.{name}]\nsource = "{source}"\ncommit = "{commit}"\n\n'


def render_packs_toml(entries: Sequence[PackEntry]) -> str:
    return "".join(render_entry(e) for e in sorted(entries, key=lambda e: e.name))


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via a unique temp file in the same directory.

    The temp name is per-call (``mkstemp``), so concurrent writers to the same path never
    consume each other's temp file — one writer's ``os.replace`` can't yank another's out.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=f"{path.suffix}.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def upsert_entry(path: Path, entry: PackEntry) -> None:
    atomic_write(
        path,
        render_packs_toml([*(e for e in read_entries(path) if e.name != entry.name), entry]),
    )


def delete_entry(path: Path, name: str) -> None:
    entries = read_entries(path)
    if name not in {e.name for e in entries}:
        raise PackError(f"pack {name!r} is not enabled in {path}")
    atomic_write(path, render_packs_toml([e for e in entries if e.name != name]))


def packs_cache_root() -> Path:
    return resolve_cache_dir() / "packs"


def meta_path(name: str) -> Path:
    return packs_cache_root() / f"{name}.meta"


def resolve_ref(source: PackSource) -> str:
    """Resolve a source's effective git ref: @latest via the latest release, else the ref or default branch."""
    match source.ref:
        case None:
            url = f"https://api.github.com/repos/{source.owner}/{source.repo}"
            return http.github_get_json(url)["default_branch"]
        case ref if ref == LATEST_REF:
            url = f"https://api.github.com/repos/{source.owner}/{source.repo}/releases/latest"
            return http.github_get_json(url)["tag_name"]
        case ref:
            return ref


def resolve_commit(source: PackSource) -> tuple[str, str]:
    """Resolve a source to its (commit sha, resolved ref) — the ref is the moving name it resolved through."""
    ref = resolve_ref(source)
    url = f"https://api.github.com/repos/{source.owner}/{source.repo}/commits/{ref}"
    return http.github_get_json(url)["sha"], ref


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


def evict_stale_commits(name: str, keep: str) -> None:
    """Best-effort GC of a pack's cached commit dirs after a fresh resolution.

    Keeps the just-resolved ``keep`` sha plus the ``KEEP_COMMITS`` most-recent other
    commit dirs by mtime, removing the rest. Never touches other packs or the dir just
    resolved; ignores every error, since a missed eviction only costs disk.

    No lock guards a sibling mid-read of an evicted dir. Recency is by mtime, which
    load_cached bumps on every cache hit, so eviction only reaches a commit that stayed
    idle past the KEEP_COMMITS buffer across intervening fetches — a >buffer-deep pin
    left unused. Racing that window is possible but accepted as best-effort.
    """
    current = f"{name}@{keep}"
    dated: list[tuple[float, Path]] = []
    try:
        candidates = list(packs_cache_root().glob(f"{name}@*"))
    except OSError:
        return
    for d in candidates:
        if d.name == current or not d.is_dir():
            continue
        with contextlib.suppress(OSError):
            dated.append((d.stat().st_mtime, d))
    dated.sort(reverse=True)
    for _, d in dated[KEEP_COMMITS:]:
        shutil.rmtree(d, ignore_errors=True)


def fetch_commit(source: PackSource, sha: str) -> ResolvedPack:
    root = packs_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / f"{sha}.lock")):
        tarball = root / f".tarball-{sha}.tar.gz"
        url = f"https://github.com/{source.owner}/{source.repo}/archive/{sha}.tar.gz"
        http.github_download(url, tarball)
        staging = root / f".staging-{sha}"
        if staging.exists():
            shutil.rmtree(staging)
        with tarfile.open(tarball) as tf:
            members = list(strip_top_level(tf))
            by_path = {m.path: m for m in members}
            manifest_member = next(
                (by_path[p] for p in (f".claude/{PACK_MANIFEST}", PACK_MANIFEST) if p in by_path), None
            )
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
    evict_stale_commits(manifest.name, sha)
    return ResolvedPack(
        ExternalPack(name=manifest.name, source=source, commit=sha), manifest.hooks_dir(final), manifest
    )


def fetch_pack(source: PackSource) -> tuple[ResolvedPack, str]:
    try:
        sha, ref = resolve_commit(source)
        return fetch_commit(source, sha), ref
    except http.GitHubFetchError as e:
        raise PackError(str(e)) from e


def add_external(source: PackSource) -> ExternalPack:
    """Fetch a source to validate and warm the cache, then return its packs.toml entry.

    A concrete ref (a tag or branch) is FROZEN: the entry carries the resolved commit,
    so packs.toml is a reproducible lockfile and runtime uses the pin directly. A moving
    ref — ``@latest`` or a bare source (default branch) — stays source-only (``commit=None``)
    and the 24h TTL keeps the resolved commit in the per-machine sidecar instead.
    """
    fetched, resolved_ref = fetch_pack(source)
    commit = fetched_commit(fetched)
    if source.ref is None or source.ref == LATEST_REF:
        PackMeta(commit=commit, checked_at=time.time(), resolved_ref=resolved_ref).write(
            meta_path(fetched.manifest.name)
        )
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
    # A cache hit never re-fetches, so touch the dir to record continued use; otherwise
    # its mtime only reflects fetch time and evict_stale_commits could reclaim a commit
    # that is long-pinned and actively loaded.
    with contextlib.suppress(OSError):
        os.utime(cached, None)
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
        sha, ref = resolve_commit(entry.source)
    except http.GitHubFetchError:
        return load_cached(entry, cached_meta.commit) if cached_meta else None
    resolved = load_cached(entry, sha) or auto_fetch(entry, sha)
    if resolved is not None:
        PackMeta(commit=sha, checked_at=now, resolved_ref=ref).write(meta_path(entry.name))
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


def resolved_ref_name(entry: ExternalPack) -> str | None:
    """The moving ref this entry last resolved to for display: None when pinned, else the sidecar's resolved_ref."""
    return None if entry.commit is not None else (m := PackMeta.load(meta_path(entry.name))) and m.resolved_ref


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
    return packs_cache_root() / f"{sha256(str(root.resolve()).encode()).hexdigest()[:16]}.resolve-fastpath"


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
            case DisabledPack():
                pass
            case ExternalPack() as ext if found := (load_cached_fresh(ext, now) if fast else resolve_external(ext)):
                resolved.append(found)
            case ExternalPack() as ext:
                missing.append(ext.name)
    if not (fast or missing):
        atomic_write(fastpath_path(root), toml_hash(root))
    return resolved, missing


def attached_path(session_dir: Path) -> Path:
    return session_dir / ATTACHED_FILE


def read_attached(session_dir: Path) -> list[AttachedPack]:
    path = attached_path(session_dir)
    if not path.exists():
        return []
    return [AttachedPack(name=e["name"], dir=e["dir"], version=e["version"]) for e in json.loads(path.read_text())]


def upsert_attached(session_dir: Path, pack: AttachedPack) -> None:
    """Record ``pack`` in the session's attach file, replacing any entry of the same name.

    The read-modify-write runs under a file lock so two ``pack attach`` processes (parallel
    SessionStart hooks) serialize rather than clobber each other's entries. When the same pack
    name re-attaches from a *different* dir the newer attach wins: a plugin update bumps its
    versioned cache dir, so the pack legitimately re-attaches from a new path on the next
    SessionStart/resume — erroring there would drop the pack for every post-update session. The
    rebind is logged at WARNING naming both dirs, since a genuine two-plugins-one-name clash
    surfaces the same way and wants a look.
    """
    path = attached_path(session_dir)
    lock = path.with_name(path.name + ".lock")
    with FileLock(str(lock)):
        existing = read_attached(session_dir)
        if (prior := next((p for p in existing if p.name == pack.name), None)) and prior.dir != pack.dir:
            logger.bind(pack=pack.name).warning(
                f"attached pack {pack.name!r} re-bound to a different dir; the newer attach wins "
                f"(was {prior.dir}, now {pack.dir})"
            )
        entries = [*(p for p in existing if p.name != pack.name), pack]
        atomic_write(
            path,
            json.dumps([{"name": p.name, "dir": p.dir, "version": p.version} for p in entries]),
        )


def resolve_attached(session_dir: Path) -> list[ResolvedPack]:
    """Resolve this session's attached packs, dropping entries that no longer resolve.

    A plugin update moves or non-atomically rewrites its versioned cache path, so a prior
    session's attach entry can dangle or point at a half-written manifest. SessionStart
    re-attaches every session, so an entry whose dir has vanished or whose manifest is
    missing/malformed is skipped with a debug log rather than killing dispatch for every
    other hook in the event — the same fail-soft shape as ``resolve_enabled_packs``.

    Packs are returned in stable name order (attach keeps one entry per name — a same-name
    re-attach replaces in place) so gate arbitration across the attached tier does not depend
    on attach timing.
    """
    resolved: list[ResolvedPack] = []
    for pack in sorted(read_attached(session_dir), key=lambda p: p.name):
        root = Path(pack.dir)
        if not root.is_dir():
            continue
        try:
            manifest = PackManifest.load(manifest_in(root))
        except (PackError, tomllib.TOMLDecodeError, KeyError, OSError):
            logger.bind(pack=pack.name, dir=pack.dir).opt(exception=True).debug(
                "skipped attached pack with a missing or malformed manifest"
            )
            continue
        resolved.append(ResolvedPack(pack, manifest.hooks_dir(root), manifest))
    return resolved
