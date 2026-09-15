"""Pack model and the two pack providers for capt-hook.

A *pack* is a collection of hook modules with an optional ``pack.toml`` descriptor. There are
exactly two providers, and nothing else:

- **Builtins** shipped inside the ``captain_hook`` wheel under ``captain_hook/builtin_packs/<name>/``,
  each carrying a ``hooks/`` dir and an optional ``pack.toml`` (only when it needs resources or tool
  specs). Identity is ``builtin:<name>``.
- **Plugin packs**: one pack per enabled Claude Code plugin, at the fixed path
  ``capt-hook/{pack.toml, hooks/}`` under the plugin root (see :mod:`captain_hook.packs.plugins`).
  Identity is the full plugin id, ``plugin:<plugin-id>`` (e.g. ``plugin:cc-context@cc-context``).

A pack's version, description, and repository derive from the wheel or the enabled-plugin roster and
``plugin.json`` — never from an authored manifest. The descriptor holds only what cannot be derived:
NLP/tool ``resources`` and declarative ``[tools]`` gate semantics.

Builtin activation is fixed policy, not per-repo config: ``fixes``/``general``/``graphite``/
``performance``/``steering`` are unconditional, and ``go``/``python`` activate when a recursive,
non-ignored build manifest (``go.mod``/``go.work``, ``pyproject.toml``) exists. There is no
``.claude/capt-hook.toml``.
"""

from __future__ import annotations

import importlib.resources
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

PACK_DESCRIPTOR = "pack.toml"
HOOKS_DIRNAME = "hooks"
# The fixed subdir under an enabled plugin's root that carries its pack (pack.toml + hooks/).
PLUGIN_PACK_DIRNAME = "capt-hook"
BUILTIN_PACKS_PACKAGE = "captain_hook.builtin_packs"

# Builtins active in every repo, no detection.
UNCONDITIONAL_BUILTINS = ("fixes", "general", "graphite", "performance", "steering")
# Language builtins activate when a recursive, non-ignored build manifest exists anywhere in the repo.
LANGUAGE_MARKERS: dict[str, tuple[str, ...]] = {
    "go": ("go.mod", "go.work"),
    "python": ("pyproject.toml",),
}
# Dirs never descended when scanning for language markers, on top of .gitignore: VCS metadata.
PRUNE_DIRS = frozenset({".git", ".jj", ".hg", ".svn"})


def pack_module_name(name: str) -> str:
    """The import-safe module component a pack loads under (non-word chars become underscores)."""
    return re.sub(r"\W", "_", name)


class PackError(Exception):
    """A pack descriptor, layout, or enabled-plugin roster was invalid."""


@dataclass(frozen=True, slots=True)
class SpanEditSpec:
    """The payload-key map a span-edit MCP tool's ``SpanEditCall`` lowering reads: the key names
    carrying the target ``path`` and written ``content``, and optionally the ``delete`` flag."""

    path: str
    content: str
    delete: str | None = None

    def as_map(self) -> dict[str, str]:
        return {"path": self.path, "content": self.content} | ({"delete": self.delete} if self.delete else {})


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A declarative MCP tool spec from a ``pack.toml`` ``[tools]`` table.

    ``name`` is the bare tool segment (e.g. ``ccx_code_edit``, ``BashFormat``) — the mount-agnostic
    form cc-transcript's matcher keys on, which matches every server prefix (``mcp__plugin_…__ccx_code_edit``
    in-repo and ``mcp__cc-context__ccx_code_edit`` elsewhere alike). ``behaves_like`` is the built-in gate
    it lowers to; ``span_edit`` is an optional span-edit key map.
    """

    name: str
    behaves_like: str
    span_edit: SpanEditSpec | None = None


def parse_span_edit(raw: object, label: str, tool: str) -> SpanEditSpec | None:
    if raw is None:
        return None
    match raw:
        case {"path": str(path), "content": str(content), **rest}:
            match rest.get("delete"):
                case str() | None as delete:
                    return SpanEditSpec(path=path, content=content, delete=delete)
                case bad:
                    raise PackError(f"[tools.{tool}] span_edit.delete in {label} must be a string, got {bad!r}")
        case dict():
            raise PackError(f"[tools.{tool}] span_edit in {label} needs string keys path/content")
        case _:
            raise PackError(f"[tools.{tool}] span_edit in {label} must be a table, got {raw!r}")


def parse_tools(raw: object, label: str) -> tuple[ToolSpec, ...]:
    if not isinstance(raw, dict):
        raise PackError(f"[tools] in {label} must be a table of tool entries, got {raw!r}")
    specs: list[ToolSpec] = []
    for tool, entry in raw.items():
        # `**rest` ignores unknown entry keys — the descriptor tolerance idiom.
        match entry:
            case {"behaves_like": str(behaves_like), **rest}:
                span_edit = parse_span_edit(rest.get("span_edit"), label, tool)
                specs.append(ToolSpec(name=tool, behaves_like=behaves_like, span_edit=span_edit))
            case dict():
                raise PackError(f"[tools.{tool}] in {label} is missing required string key behaves_like")
            case _:
                raise PackError(f"[tools.{tool}] in {label} must be a table, got {entry!r}")
    return tuple(specs)


def parse_resources(raw: object, label: str) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise PackError(f"resources in {label} must be a list of resource strings, got {raw!r}")
    for name in raw:
        if not isinstance(name, str):
            raise PackError(f"resource {name!r} in {label} must be a string")
    return tuple(raw)


@dataclass(frozen=True, slots=True)
class PackDescriptor:
    """The optional ``pack.toml`` next to a pack's ``hooks/`` — only what can't be derived.

    ``resources`` names NLP/tool resources the pack's hooks need provisioned (e.g.
    ``"spacy:en_core_web_sm"``, ``"wordnet:oewn:2025"``); ``tools`` declares MCP-tool gate semantics.
    An absent descriptor is the empty one.
    """

    resources: tuple[str, ...] = ()
    tools: tuple[ToolSpec, ...] = ()

    @classmethod
    def load(cls, path: Path) -> PackDescriptor:
        if not path.is_file():
            return cls()
        try:
            doc = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise PackError(f"invalid pack descriptor {path}: {e}") from e
        return cls(
            resources=parse_resources(doc.get("resources", ()), str(path)),
            tools=parse_tools(doc.get("tools", {}), str(path)),
        )


@dataclass(frozen=True, slots=True)
class BuiltinPack:
    """A pack shipped in the ``captain_hook`` wheel; identity ``builtin:<name>``."""

    name: str

    @property
    def pack_id(self) -> str:
        return f"builtin:{self.name}"


@dataclass(frozen=True, slots=True)
class PluginPack:
    """A pack shipped by an enabled Claude plugin at ``capt-hook/`` under its root.

    ``plugin_id`` is the full Claude plugin id (``cc-context@cc-context``) — the globally-unique
    identity and the module-namespace source; ``root`` is the plugin install dir; ``repository`` is
    the ``plugin.json`` repository url a misfire's fix PR routes to, or ``None``.
    """

    plugin_id: str
    root: str
    repository: str | None = None

    @property
    def pack_id(self) -> str:
        return f"plugin:{self.plugin_id}"

    @property
    def name(self) -> str:
        return self.plugin_id


type PackEntry = BuiltinPack | PluginPack


@dataclass(frozen=True, slots=True)
class ResolvedPack:
    """A pack resolved to its on-disk ``hooks/`` dir and descriptor, ready to load."""

    entry: PackEntry
    path: Path
    descriptor: PackDescriptor

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def pack_id(self) -> str:
        return self.entry.pack_id


def builtin_packs_root() -> Path:
    return Path(str(importlib.resources.files(BUILTIN_PACKS_PACKAGE)))


def builtin_names() -> tuple[str, ...]:
    """Every builtin pack shipped in the wheel — a ``builtin_packs/<name>/`` dir with a ``hooks/`` subdir."""
    root = builtin_packs_root()
    return tuple(sorted(p.name for p in root.iterdir() if p.is_dir() and (p / HOOKS_DIRNAME).is_dir()))


def resolve_builtin(name: str) -> ResolvedPack:
    pack_root = builtin_packs_root() / name
    if not (hooks := pack_root / HOOKS_DIRNAME).is_dir():
        raise PackError(f"unknown builtin pack {name!r}; available: {', '.join(builtin_names()) or 'none'}")
    return ResolvedPack(BuiltinPack(name=name), hooks, PackDescriptor.load(pack_root / PACK_DESCRIPTOR))


def gitignore_lines(path: Path) -> list[str]:
    """The non-comment, non-blank pattern lines of a ``.gitignore``, or ``[]`` when it is unreadable.

    A walk root wide enough to reach a synthetic path — macOS mounts ``/.resolve`` under ``/`` —
    stats it with ``EINVAL`` rather than a missing-file error, which ``is_file`` propagates.
    """
    try:
        if not path.is_file():
            return []
    except OSError:
        return []
    return [line for raw in path.read_text().splitlines() if (line := raw.strip()) and not line.startswith("#")]


def anchor_pattern(line: str, rel_dir: str) -> str:
    """Rewrite a ``.gitignore`` pattern from a subdirectory so it matches paths relative to the walk root.

    An anchored pattern (a slash before its last character) binds to ``rel_dir``; an unanchored one
    matches at any depth beneath it, so it becomes ``rel_dir/**/pattern``. A leading ``!`` is preserved.
    """
    negate = line.startswith("!")
    pat = line[1:] if negate else line
    prefix = "!" if negate else ""
    tail = pat.lstrip("/")
    return f"{prefix}{rel_dir}/{tail}" if "/" in pat.rstrip("/") else f"{prefix}{rel_dir}/**/{tail}"


def descend_spec(parent: GitIgnoreSpec | None, own: list[str], rel_dir: str) -> GitIgnoreSpec | None:
    """The gitignore spec governing ``rel_dir``: ``parent`` extended by that directory's own patterns.

    Compiling the accumulated pattern set per directory is quadratic in a deep tree, so a directory
    that carries no ``.gitignore`` reuses its parent's compiled spec and one that does compiles only
    its own lines and concatenates the compiled patterns — the same last-match-wins order, and the
    same result as compiling the accumulation from scratch.
    """
    if not own:
        return parent
    added = GitIgnoreSpec.from_lines(own if not rel_dir else [anchor_pattern(line, rel_dir) for line in own])
    return added if parent is None else parent + added


def detect_languages(root: Path) -> frozenset[str]:
    """The languages in :data:`LANGUAGE_MARKERS` whose recursive, non-``.gitignore``d build manifest is under ``root``.

    Walks the tree once with real gitignore semantics: patterns accumulate down the tree so a nested
    ``.gitignore`` governs its own subtree, and anchored/path/negation patterns resolve through
    :class:`pathspec.GitIgnoreSpec`. VCS-metadata dirs and gitignored dirs are pruned, and each marker
    file is itself checked against the accumulated spec, so an individually-ignored ``go.mod`` never
    counts. Each language short-circuits on its first surviving marker; the walk stops once all are found.
    """
    pending = dict(LANGUAGE_MARKERS)
    found: set[str] = set()
    specs: dict[str, GitIgnoreSpec | None] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        rel_dir = "" if rel == "." else rel
        spec = specs[dirpath] = descend_spec(
            specs.get(os.path.dirname(dirpath)),
            gitignore_lines(Path(dirpath) / ".gitignore"),
            rel_dir,
        )
        for lang, markers in list(pending.items()):
            if any(
                m in filenames and (spec is None or not spec.match_file(f"{rel_dir}/{m}" if rel_dir else m))
                for m in markers
            ):
                found.add(lang)
                del pending[lang]
        if not pending:
            break
        dirnames[:] = [
            d
            for d in dirnames
            if d not in PRUNE_DIRS
            and not (spec is not None and spec.match_file(f"{rel_dir}/{d}/" if rel_dir else f"{d}/"))
        ]
    return frozenset(found)


def active_builtins(root: Path) -> tuple[str, ...]:
    """The builtin pack names active for ``root``: the unconditional ones plus detected languages, name-ordered."""
    return tuple(sorted({*UNCONDITIONAL_BUILTINS, *detect_languages(root)}))
