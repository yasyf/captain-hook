"""Pack model, manifest IO, GitHub fetch, and resolution for capt-hook packs.

A *pack* is a named, versioned collection of hooks. Builtin packs ship inside the
``captain_hook`` wheel under ``captain_hook/packs/<name>/``; external packs live in a
GitHub repo carrying a ``capt-hook.toml`` manifest and are fetched as a tarball into a
local cache. A single ``.claude/capt-hook.toml`` carries both config axes: a ``[pack]``
table is the manifest (``name``/``description``/``hooks``, plus optional ``version``,
``nlp``, and dependency ``marketplaces``) that makes a directory a pack, and
``[packs.<name>]`` tables are a project's enablement list. The two are independent — a
consumer file holds only ``[packs.*]`` and no ``[pack]``, and a pack that no project has
enabled ships only ``[pack]``.

An enablement entry may pin a ``commit`` (a hard lock used directly) or carry only a
``source`` whose ref moves: ``@latest`` (the latest GitHub release), a branch, or the bare
default branch. A moving ref re-resolves to a commit at most once per 24h, tracked
alongside the resolved commit in a per-machine ``PackMeta`` sidecar; within the window the
cached commit is used with no network. A declared pack missing from the cache is fetched on
demand, so the first event after a clone or a moved ref self-heals. The only loud failure
is a pack that is both uncached and unreachable.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import json
import os
import posixpath
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

from captain_hook.util import http
from captain_hook.util.fs import atomic_write
from captain_hook.util.paths import resolve_cache_dir

PACK_MANIFEST = "capt-hook.toml"
SHA_MARKER = ".sha"
LATEST_REF = "latest"
# Moving refs (@latest / a branch / a bare default-branch source) re-resolve to a fresh
# commit at most once per this window; within it the cached commit is used with no network.
REFRESH_TTL_SECONDS = 24 * 60 * 60
PACK_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")
# A dependency marketplace `owner/repo` slug: one slash, no leading `-` either side (can't pose as a
# `claude` flag), ASCII char classes only (not `\w`) so a homoglyph slug can't slip through.
MARKETPLACE_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
# Cached commit dirs kept per pack besides the just-resolved one: a recency buffer
# so a rollback or a still-loading prior session's pin survives one fresh fetch.
KEEP_COMMITS = 2
# Shared manifest-resolution order (see resolve_manifest): (pack_root, manifest) paths relative to
# the search root — .claude/ then the bare file, at the root then one hooks/ level below.
MANIFEST_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("", f".claude/{PACK_MANIFEST}"),
    ("", PACK_MANIFEST),
    ("hooks", f"hooks/.claude/{PACK_MANIFEST}"),
    ("hooks", f"hooks/{PACK_MANIFEST}"),
)


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


def parse_marketplaces(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise PackError(f"marketplaces must be a list of owner/repo slugs, got {raw!r}")
    for slug in raw:
        if not isinstance(slug, str):
            raise PackError(f"marketplace repo {slug!r} must be a string owner/repo slug")
        if not MARKETPLACE_REPO_RE.fullmatch(slug):
            raise PackError(f"marketplace repo {slug!r} must match {MARKETPLACE_REPO_RE.pattern}")
    return tuple(raw)


@dataclass(frozen=True, slots=True)
class SpanEditSpec:
    """The payload-key map a span-edit MCP tool's ``SpanEditCall`` lowering reads: the key names
    carrying the target ``path`` and written ``content``, and optionally the ``delete`` flag."""

    path: str
    content: str
    delete: str | None = None

    def as_map(self) -> dict[str, str]:
        m = {"path": self.path, "content": self.content}
        if self.delete is not None:
            m["delete"] = self.delete
        return m


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A declarative MCP tool spec from a pack manifest's top-level ``[tools]`` table: the bare tool
    segment, the built-in gate it ``behaves_like``, and an optional span-edit lowering."""

    name: str
    behaves_like: str
    span_edit: SpanEditSpec | None = None


def parse_span_edit(raw: object, pack: str, tool: str) -> SpanEditSpec | None:
    if raw is None:
        return None
    match raw:
        case {"path": str(path), "content": str(content), **rest}:
            match rest.get("delete"):
                case str() | None as delete:
                    return SpanEditSpec(path=path, content=content, delete=delete)
                case bad:
                    raise PackError(f"[tools.{tool}] span_edit.delete in pack {pack!r} must be a string, got {bad!r}")
        case dict():
            raise PackError(f"[tools.{tool}] span_edit in pack {pack!r} needs string keys path/content")
        case _:
            raise PackError(f"[tools.{tool}] span_edit in pack {pack!r} must be a table, got {raw!r}")


def parse_tools(raw: object, pack: str) -> tuple[ToolSpec, ...]:
    if not isinstance(raw, dict):
        raise PackError(f"[tools] in pack {pack!r} must be a table of tool entries, got {raw!r}")
    specs: list[ToolSpec] = []
    for tool, entry in raw.items():
        # `**rest` ignores unknown entry keys — the PackManifest.load tolerance idiom.
        match entry:
            case {"behaves_like": str(behaves_like), **rest}:
                span_edit = parse_span_edit(rest.get("span_edit"), pack, tool)
                specs.append(ToolSpec(name=tool, behaves_like=behaves_like, span_edit=span_edit))
            case dict():
                raise PackError(f"[tools.{tool}] in pack {pack!r} is missing required string key behaves_like")
            case _:
                raise PackError(f"[tools.{tool}] in pack {pack!r} must be a table, got {entry!r}")
    return tuple(specs)


@dataclass(frozen=True, slots=True)
class PackManifest:
    name: str
    description: str
    hooks: str
    version: str = "0.0.0"
    nlp: bool = False
    marketplaces: tuple[str, ...] = ()
    tools: tuple[ToolSpec, ...] = ()

    @classmethod
    def load(cls, path: Path) -> PackManifest:
        if not path.is_file():
            raise PackError(f"pack manifest {PACK_MANIFEST} missing at {path.parent}")
        # `**rest` ignores unknown [pack] keys; every other failure mode raises PackError. [tools]
        # is a top-level table (a sibling of [pack]) read from the same parsed doc.
        doc = tomllib.loads(path.read_text())
        match doc.get("pack"):
            case {"name": str(name), "description": str(description), "hooks": str(hooks), **rest}:
                manifest = cls(
                    name=name,
                    description=description,
                    hooks=hooks,
                    version=rest.get("version", "0.0.0"),
                    nlp=rest.get("nlp", False),
                    marketplaces=parse_marketplaces(rest.get("marketplaces", ())),
                    tools=parse_tools(doc.get("tools", {}), name),
                )
            case dict():
                raise PackError(f"[pack] in {path} is missing required string keys name/description/hooks")
            case _:
                raise PackError(f"pack manifest {path} has no [pack] section")
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
    # commit lives in a per-machine sidecar (see PackMeta), not in the committed config.
    commit: str | None = None


@dataclass(frozen=True, slots=True)
class DisabledPack:
    """A ``[packs.<name>]`` config entry that declines a pack by name.

    ``[packs.<name>] disabled = true`` suppresses a pack the repo would otherwise
    inherit — a builtin, an external source, or a discovered plugin pack — regardless of
    any other keys on the entry. Disabling always wins.
    """

    name: str


@dataclass(frozen=True, slots=True)
class PluginPack:
    """A pack discovered on an enabled Claude Code plugin whose root ships a ``[pack]`` manifest.

    ``plugin_id`` is the Claude plugin id it was discovered under, ``dir`` the absolute pack
    root holding the ``capt-hook.toml`` manifest, and ``version`` the manifest version.
    """

    name: str
    plugin_id: str
    dir: str
    version: str


type PackEntry = BuiltinPack | ExternalPack | DisabledPack
type ResolvedEntry = BuiltinPack | ExternalPack | PluginPack


@dataclass(frozen=True, slots=True)
class ResolvedPack:
    entry: ResolvedEntry
    path: Path
    manifest: PackManifest


@dataclass(frozen=True, slots=True)
class PackMeta:
    """Per-machine resolution sidecar for a moving-ref pack: the last-resolved commit, ref, and when.

    Stored as JSON next to the cache so the resolved ``commit``, ``resolved_ref``, and
    ``checked_at`` timestamp never enter the committed config. ``checked_at`` gates
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


def config_path(root: Path) -> Path:
    return root / ".claude" / PACK_MANIFEST


def text_has_pack_section(text: str) -> bool:
    """True when ``text`` is valid TOML declaring a ``[pack]`` table (malformed TOML reads as False)."""
    with contextlib.suppress(tomllib.TOMLDecodeError):
        return isinstance(tomllib.loads(text).get("pack"), dict)
    return False


def has_pack_section(path: Path) -> bool:
    """True when ``path`` is a readable file whose TOML declares a ``[pack]`` table."""
    with contextlib.suppress(OSError):
        return path.is_file() and text_has_pack_section(path.read_text())
    return False


@dataclass(frozen=True, slots=True)
class ManifestLocation:
    """A resolved pack manifest: the ``capt-hook.toml`` file and the ``pack_root`` its ``hooks`` resolves against."""

    pack_root: Path
    manifest: Path


def resolve_manifest(root: Path) -> ManifestLocation:
    """Resolve ``root``'s pack manifest across the four discovery layouts, in :data:`MANIFEST_CANDIDATES` order.

    Probes ``.claude/capt-hook.toml`` then the bare ``capt-hook.toml``, at ``root`` then one ``hooks/``
    level below. The first candidate whose file carries a ``[pack]`` section wins — so a consumer-only
    ``.claude`` file (``[packs.*]`` enablement with no ``[pack]``, as ``pack add`` writes) never shadows
    a real ``[pack]`` at the root or under ``hooks/``. When none carries ``[pack]`` the first candidate
    whose file merely exists is the error-reporting fallback, else the bare root ``capt-hook.toml`` — so
    ``PackManifest.load`` still fails loudly at the canonical missing location.
    """
    located = [(root / pack_root, root / manifest) for pack_root, manifest in MANIFEST_CANDIDATES]
    pack_root, manifest = next((pair for pair in located if has_pack_section(pair[1])), None) or next(
        (pair for pair in located if pair[1].is_file()), (root, root / PACK_MANIFEST)
    )
    return ManifestLocation(pack_root=pack_root, manifest=manifest)


def manifest_in(root: Path) -> Path:
    """The pack manifest path under ``root`` — the winning candidate from :func:`resolve_manifest`."""
    return resolve_manifest(root).manifest


def manifest_at(path: Path) -> PackManifest | None:
    """Load the ``[pack]`` manifest at an explicit ``capt-hook.toml`` path, or ``None`` when it is absent or
    carries no ``[pack]`` section (a consumer-only ``[packs.*]`` file). A malformed ``[pack]`` still raises.
    """
    if not path.is_file() or not isinstance(tomllib.loads(path.read_text()).get("pack"), dict):
        return None
    return PackManifest.load(path)


def load_pack_manifest(root: Path) -> PackManifest | None:
    """Load ``root``'s ``[pack]`` manifest across the discovery layouts, or ``None`` when there is none.

    The discovery probe: ``None`` when the resolved manifest is absent or present without a ``[pack]``
    section (a consumer-only file). A present-but-malformed ``[pack]`` still raises ``PackError``.
    """
    return manifest_at(resolve_manifest(root).manifest)


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


def read_config_entries(root: Path) -> list[PackEntry]:
    return read_entries(config_path(root))


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


def strip_packs_tables(text: str) -> str:
    """Return ``text`` with every ``[packs]`` / ``[packs.*]`` table dropped, all else verbatim.

    A section-scoped rewrite: a ``[pack]`` manifest table, comments, and preamble outside the
    enablement tables survive byte-for-byte, so an upsert re-renders only the ``[packs.*]`` tables.
    """
    kept: list[str] = []
    dropping = False
    for line in text.splitlines(keepends=True):
        if (stripped := line.lstrip()).startswith("[["):
            dropping = False
        elif stripped.startswith("["):
            name = stripped[1:].partition("]")[0].strip()
            dropping = name == "packs" or name.startswith("packs.")
        if not dropping:
            kept.append(line)
    return "".join(kept)


def verify_config_rewrite(path: Path, original: str, new_text: str, entries: Sequence[PackEntry]) -> None:
    """Refuse a ``[packs.*]`` rewrite that would corrupt the file, before any bytes reach disk.

    ``strip_packs_tables`` is line-based, so any top-level multiline string whose lines mimic a
    ``[packs.*]`` header can make it drop real content. This is the guard, not a smarter parser:
    re-parse the rewritten text and refuse it — raising ``PackError`` — unless (a) it parses, (b) the
    whole document minus its ``packs`` key is byte-for-byte the parsed original minus ``packs``, and
    (c) its ``[packs]`` table equals exactly the entries asked for. Every refusal points at a hand edit.
    """
    intended = tomllib.loads(render_packs_toml(entries)).get("packs", {})
    try:
        result = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise PackError(f"refusing to rewrite {path}: the result would not parse ({e}); hand-edit it instead") from e
    before = tomllib.loads(original) if original else {}
    if {k: v for k, v in result.items() if k != "packs"} != {k: v for k, v in before.items() if k != "packs"}:
        raise PackError(
            f"refusing to rewrite {path}: content outside the [packs.*] tables would change; hand-edit it instead"
        )
    if result.get("packs", {}) != intended:
        raise PackError(
            f"refusing to rewrite {path}: its [packs.*] tables would not match the intended entries; "
            "hand-edit it instead"
        )


def write_config_entries(path: Path, entries: Sequence[PackEntry]) -> None:
    original = path.read_text() if path.exists() else ""
    preserved = strip_packs_tables(original).rstrip("\n") if original else ""
    rendered = render_packs_toml(entries)
    new_text = f"{preserved}\n\n{rendered}" if preserved else rendered
    verify_config_rewrite(path, original, new_text, entries)
    atomic_write(path, new_text)


def upsert_entry(path: Path, entry: PackEntry) -> None:
    write_config_entries(path, [*(e for e in read_entries(path) if e.name != entry.name), entry])


def delete_entry(path: Path, name: str) -> None:
    entries = read_entries(path)
    if name not in {e.name for e in entries}:
        raise PackError(f"pack {name!r} is not enabled in {path}")
    write_config_entries(path, [e for e in entries if e.name != name])


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


def members_under(
    members: list[tarfile.TarInfo], pack_root: str, hooks: str, manifest_path: str
) -> Iterator[tarfile.TarInfo]:
    """Yield the manifest plus members within the pack's hooks dir.

    ``pack_root`` is the archive-relative dir the manifest resolves under ("" at the archive root,
    "hooks" one level down); ``hooks`` is the manifest's hooks field, relative to it. The combined
    hooks dir of "." (hooks beside the manifest) selects the whole pack subtree; a real subdir selects
    only that subtree, so the cache holds just what the loader imports. The manifest is included by its
    actual archive path so a nested manifest survives without dragging in its siblings.
    """
    rel = "" if (combined := posixpath.normpath(posixpath.join(pack_root, hooks))) == "." else combined
    prefix = "" if rel == "" else rel + "/"
    for m in members:
        if m.path == manifest_path or not prefix or m.path == rel or m.path.startswith(prefix):
            yield m


def resolve_manifest_member(
    by_path: dict[str, tarfile.TarInfo], tf: tarfile.TarFile
) -> tuple[str, tarfile.TarInfo] | None:
    """Pick the ``(pack_root, member)`` manifest from a tarball's members in :data:`MANIFEST_CANDIDATES` order.

    Mirrors :func:`resolve_manifest` over archive member names: the first candidate present and carrying
    a ``[pack]`` section wins; when none carries ``[pack]`` the first candidate present is the fallback,
    and ``None`` means no candidate member exists at all.
    """
    located = [(pack_root, by_path[manifest]) for pack_root, manifest in MANIFEST_CANDIDATES if manifest in by_path]
    if not located:
        return None
    return next((pair for pair in located if member_has_pack_section(tf, pair[1])), located[0])


def member_has_pack_section(tf: tarfile.TarFile, member: tarfile.TarInfo) -> bool:
    if (extracted := tf.extractfile(member)) is None:
        return False
    with extracted:
        return text_has_pack_section(extracted.read().decode(errors="replace"))


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
            if (located := resolve_manifest_member({m.path: m for m in members}, tf)) is None:
                raise PackError(f"pack manifest {PACK_MANIFEST} missing in {source}")
            pack_root, manifest_member = located
            tf.extract(manifest_member, staging, filter="data")
            manifest = PackManifest.load(staging / manifest_member.path)
            tf.extractall(
                staging,
                members=list(members_under(members, pack_root, manifest.hooks, manifest_member.path)),
                filter="data",
            )
        tarball.unlink()
        final = root / f"{manifest.name}@{sha}"
        if final.exists():
            shutil.rmtree(final)
        os.replace(staging, final)
        (final / SHA_MARKER).write_text(sha)
    evict_stale_commits(manifest.name, sha)
    return ResolvedPack(
        ExternalPack(name=manifest.name, source=source, commit=sha), manifest.hooks_dir(final / pack_root), manifest
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
    location = resolve_manifest(pack_dir)
    manifest = PackManifest.load(location.manifest)
    return ResolvedPack(BuiltinPack(name=name), manifest.hooks_dir(location.pack_root), manifest)


def load_cached(entry: ExternalPack, sha: str) -> ResolvedPack | None:
    if not (cached := find_cached(entry.name, sha)):
        return None
    # A cache hit never re-fetches, so touch the dir to record continued use; otherwise
    # its mtime only reflects fetch time and evict_stale_commits could reclaim a commit
    # that is long-pinned and actively loaded.
    with contextlib.suppress(OSError):
        os.utime(cached, None)
    location = resolve_manifest(cached)
    manifest = PackManifest.load(location.manifest)
    return ResolvedPack(entry, manifest.hooks_dir(location.pack_root), manifest)


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
    path = config_path(root)
    return sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def resolve_enabled_packs(root: Path, entries: Sequence[PackEntry]) -> tuple[list[ResolvedPack], list[str]]:
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
