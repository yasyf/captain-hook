"""Discovery of the pack shipped on each enabled Claude Code plugin of a project.

A pack-shipping Claude plugin carries its pack at the fixed path ``capt-hook/{pack.toml, hooks/}``
under the plugin root; the dispatcher loads every enabled plugin whose root ships one. Enabled
plugins come from ``claude plugin list --json`` — the sanctioned interface, which resolves
``enabled`` against the project's settings stack — but that CLI spawns Node (~1s), far too slow
for the ~3ms dispatch hot path. So the roster is cached per project in a ``<key>.plugins``
snapshot, invalidated by the stat tuple of the files that shape it: Claude Code's
``installed_plugins.json`` (mtime only — undocumented, never parsed), the user settings, and the
project's two settings files. A watched-file change (a plugin install, an enable or disable) re-runs
the CLI once; every other event reads the snapshot. The fixed-path probe itself runs live per
discovery (cheap stats), so a pack edit inside a plugin dir needs no snapshot invalidation.

Pack load is all-or-nothing: a plugin advertising ``capt-hook/`` with a missing descriptor or hooks
dir raises rather than silently dropping guards. The CLI's roster is machine-wide — a ``project`` or
``local`` entry names the ``projectPath`` it was installed under, and one plugin id is legitimately
installed at the same scope in many projects — so the roster is first scoped to the discovering root
and then deduped to one entry per full plugin id by scope precedence (``local`` > ``project`` >
``user``); two install paths at the same scope *within one project* is a corrupt roster.

A roster that cannot be enumerated raises :class:`PluginListError` and caches nothing. An empty
roster means "this project enables no pack-shipping plugin" and must never also mean "the roster
could not be read": :meth:`~captain_hook.cli.CliState.plugin_packs` is the one seam that catches the
failure, and it records it where a person looks instead of discarding the whole pack layer silently.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from shutil import which

from filelock import FileLock, Timeout
from loguru import logger

from captain_hook.packs import manager
from captain_hook.util.fs import atomic_write, read_json
from captain_hook.util.paths import resolve_cache_dir, resolve_claude_config_dir

CLI_TIMEOUT_SECONDS = 60
FAILURE_TTL = timedelta(seconds=15)
GATE_TIMEOUT_SECONDS = 10
GATE_WIDTH = 4
GATE_POLL_SECONDS = 0.05
# Bumped whenever a snapshot's meaning changes, so every older one is recomputed instead of served.
SNAPSHOT_VERSION = 2
# Claude Code layers plugin scopes; when one id resolves at more than one scope the earlier entry here
# outranks the later, mirroring settings precedence. An unknown scope ranks lowest.
SCOPE_PRECEDENCE = ("local", "project", "user")
# Enterprise managed settings can enable/disable plugins; Claude Code reads them from fixed OS
# locations (macOS/Linux) plus the config dir, each with a sibling managed-settings.d drop-in dir.
MANAGED_SETTINGS_DIRS = (
    Path("/Library/Application Support/ClaudeCode"),
    Path("/etc/claude-code"),
)

type StatRecord = tuple[str, int, int, int] | tuple[str, None]


def managed_settings_dirs(config: Path) -> tuple[Path, ...]:
    return (config, *MANAGED_SETTINGS_DIRS)


class PluginListError(Exception):
    """``claude plugin list --json`` exited nonzero or returned unusable output."""


class RosterUnavailable(PluginListError):
    """The CLI could not be run at all, rather than running and answering unusably.

    The one failure that says nothing about the project: a machine out of fork capacity fails it for
    every root at once. It is recorded machine-wide so the roots queued behind the gate are told
    rather than each re-spawning, which is what turns a fork shortage into a sustained storm.
    """


def installed_plugins_path() -> Path:
    return resolve_claude_config_dir() / "plugins" / "installed_plugins.json"


def watched_paths(root: Path) -> tuple[Path, ...]:
    config = resolve_claude_config_dir()
    return (
        installed_plugins_path(),
        config / "settings.json",
        *(d / "managed-settings.json" for d in managed_settings_dirs(config)),
        root / ".claude" / "settings.json",
        root / ".claude" / "settings.local.json",
    )


def stat_record(path: Path) -> StatRecord:
    abs_path = os.path.abspath(path)
    try:
        st = path.stat()
    except OSError:
        return (abs_path, None)
    # ctime is folded in alongside mtime+size: a same-size, mtime-restored rewrite moves only ctime,
    # and without it the roster snapshot would stay "fresh" across such an edit.
    return (abs_path, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def dropin_records(dropin_dir: Path) -> tuple[StatRecord, ...]:
    """A ``managed-settings.d`` dir's own stat plus one per contained ``*.json`` (sorted); a lone
    ``(path, None)`` when the dir is absent. The dir stat catches drop-in adds/removes, the per-file
    records catch edits."""
    if not dropin_dir.is_dir():
        return (stat_record(dropin_dir),)
    return (stat_record(dropin_dir), *(stat_record(p) for p in sorted(dropin_dir.glob("*.json"))))


def stat_records(root: Path) -> tuple[StatRecord, ...]:
    config = resolve_claude_config_dir()
    dropins = tuple(rec for d in managed_settings_dirs(config) for rec in dropin_records(d / "managed-settings.d"))
    return tuple(stat_record(p) for p in watched_paths(root)) + dropins


def snapshot_cache_root() -> Path:
    return resolve_cache_dir() / "packs"


def snapshot_path(root: Path) -> Path:
    return snapshot_cache_root() / f"{sha256(str(root.resolve()).encode()).hexdigest()[:16]}.plugins"


def failure_path(root: Path) -> Path:
    return snapshot_path(root).with_suffix(".failure")


def gate_path(slot: int) -> Path:
    return snapshot_cache_root() / f"roster.gate.{slot}"


def outage_path() -> Path:
    return snapshot_cache_root() / "roster.outage"


@contextmanager
def roster_gate() -> Iterator[None]:
    """Admit at most :data:`GATE_WIDTH` roster CLI spawns machine-wide, degrading rather than failing.

    A watched file is machine-wide, so one change invalidates every root's snapshot at once and every
    session spawns Node together — the stampede that exhausts fork capacity. Holding a slot while
    spawning bounds that instead. A slot is one ``flock``, which the kernel drops when its holder
    dies, so a crashed session cannot wedge the machine; a machine still busy past
    :data:`GATE_TIMEOUT_SECONDS` is overtaken rather than waited on.
    """
    deadline = time.monotonic() + GATE_TIMEOUT_SECONDS
    while True:
        for slot in range(GATE_WIDTH):
            lock = FileLock(str(gate_path(slot)), timeout=0)
            try:
                lock.acquire()
            except Timeout:
                continue
            try:
                yield
            finally:
                lock.release()
            return
        if time.monotonic() >= deadline:
            yield
            return
        time.sleep(GATE_POLL_SECONDS)


@dataclass(frozen=True, slots=True)
class EnabledPlugin:
    id: str
    version: str
    root: str
    scope: str | None = None
    project_path: str | None = None


@dataclass(frozen=True, slots=True)
class PluginSnapshot:
    stat: tuple[StatRecord, ...]
    plugins: tuple[EnabledPlugin, ...]

    @classmethod
    def load(cls, path: Path) -> PluginSnapshot | None:
        """The snapshot at ``path``, or ``None`` when it is absent, unreadable, or an older format.

        The version gate is load-bearing rather than housekeeping: a snapshot written before the
        roster was scoped per project holds entries this root may not own, and a snapshot written by
        the discarded warning-and-empty path holds a failure posing as an answer. Neither may be
        served, and neither moves the stat tuple that would otherwise invalidate it.
        """
        if (data := read_json(path)) is None or data.get("version") != SNAPSHOT_VERSION:
            return None
        try:
            return cls(
                stat=tuple(tuple(rec) for rec in data["stat"]),
                plugins=tuple(
                    EnabledPlugin(
                        id=p["id"],
                        version=p["version"],
                        root=p["root"],
                        scope=p.get("scope"),
                        project_path=p["project_path"],
                    )
                    for p in data["plugins"]
                ),
            )
        except (KeyError, TypeError):
            return None

    def write(self, path: Path) -> None:
        atomic_write(
            path,
            json.dumps(
                {
                    "version": SNAPSHOT_VERSION,
                    "stat": [list(rec) for rec in self.stat],
                    "plugins": [
                        {
                            "id": p.id,
                            "version": p.version,
                            "root": p.root,
                            "scope": p.scope,
                            "project_path": p.project_path,
                        }
                        for p in self.plugins
                    ],
                }
            ),
        )

    def fresh(self, records: tuple[StatRecord, ...]) -> bool:
        return self.stat == records


@dataclass(frozen=True, slots=True)
class RosterFailure:
    """A recorded enumeration failure, so an unreadable roster costs one CLI spawn per TTL.

    This is the negative half of the cache and never a roster: :func:`enabled_plugins` re-raises from
    it rather than serving anything, so a failure stays a failure. "This project enables no
    pack-shipping plugin" is a different fact and reaches disk only as a :class:`PluginSnapshot` — an
    empty roster is never recorded here, and a failure is never recorded there.

    Freshness is bounded twice. The stat tuple retires the record the moment a watched file moves, so
    a fixed roster is picked up at once rather than after the timer; :data:`FAILURE_TTL` retires it
    otherwise, so a transient failure — the fork exhaustion this exists for — recovers on its own.
    """

    stat: tuple[StatRecord, ...]
    at: datetime
    message: str

    @classmethod
    def load(cls, path: Path) -> RosterFailure | None:
        if (data := read_json(path)) is None or data.get("version") != SNAPSHOT_VERSION:
            return None
        try:
            return cls(
                stat=tuple(tuple(rec) for rec in data["stat"]),
                at=datetime.fromisoformat(data["at"]),
                message=str(data["message"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def write(self, path: Path) -> None:
        atomic_write(
            path,
            json.dumps(
                {
                    "version": SNAPSHOT_VERSION,
                    "stat": [list(rec) for rec in self.stat],
                    "at": self.at.isoformat(),
                    "message": self.message,
                }
            ),
        )

    def fresh(self, records: tuple[StatRecord, ...], now: datetime) -> bool:
        return self.stat == records and now - self.at < FAILURE_TTL


@dataclass(frozen=True, slots=True)
class RosterOutage:
    """A machine-wide record that the CLI could not be spawned, bounded by :data:`FAILURE_TTL`.

    Carries no roster and no stat tuple, because it records a condition of the machine rather than of
    any project — so it never stands in for a root's answer. Enablement is resolved against the
    project's settings stack and differs between roots, so it stays per root in
    :class:`PluginSnapshot`; only the inability to ask at all is shared.
    """

    at: datetime
    message: str

    @classmethod
    def load(cls, path: Path) -> RosterOutage | None:
        if (data := read_json(path)) is None or data.get("version") != SNAPSHOT_VERSION:
            return None
        try:
            return cls(at=datetime.fromisoformat(data["at"]), message=str(data["message"]))
        except (KeyError, TypeError, ValueError):
            return None

    def write(self, path: Path) -> None:
        atomic_write(
            path, json.dumps({"version": SNAPSHOT_VERSION, "at": self.at.isoformat(), "message": self.message})
        )

    def fresh(self, now: datetime) -> bool:
        return now - self.at < FAILURE_TTL


def parse_plugin_entry(entry: object) -> EnabledPlugin | None:
    """One roster entry → an :class:`EnabledPlugin`, or ``None`` to skip it (unusable, not enabled).

    A non-object entry, or one whose ``id``/``installPath`` is missing or not a string, is skipped with
    a debug line so a single malformed sibling never suppresses the valid ones; an entry Claude Code did
    not report ``enabled`` with an install path is skipped silently (the ordinary not-enabled case).
    ``projectPath`` is the project a ``project``/``local`` entry belongs to; a ``user``-scope entry
    carries none.
    """
    if not isinstance(entry, dict):
        logger.debug(f"skipping non-object plugin roster entry {entry!r}")
        return None
    if entry.get("enabled") is not True or not entry.get("installPath"):
        return None
    if not (isinstance(pid := entry.get("id"), str) and pid and isinstance(path := entry.get("installPath"), str)):
        logger.bind(id=entry.get("id"), installPath=entry.get("installPath")).debug(
            "skipping plugin roster entry lacking a string id/installPath"
        )
        return None
    version = entry.get("version", "")
    scope = entry.get("scope")
    project = entry.get("projectPath")
    return EnabledPlugin(
        id=pid,
        version=version if isinstance(version, str) else "",
        root=path,
        scope=scope if isinstance(scope, str) else None,
        project_path=project if isinstance(project, str) and project else None,
    )


def scope_rank(scope: str | None) -> int:
    return SCOPE_PRECEDENCE.index(scope) if scope in SCOPE_PRECEDENCE else len(SCOPE_PRECEDENCE)


def governs(plugin: EnabledPlugin, root: Path) -> bool:
    """Whether a roster entry governs ``root``.

    ``claude plugin list`` answers machine-wide whatever directory it runs in: a ``project`` or
    ``local`` entry carries the ``projectPath`` it was installed under and governs that project
    alone, while a ``user``-scope entry carries none and governs every root. Without this the same
    plugin installed at ``local`` scope in six projects reads as six competing installs of one id.
    """
    return plugin.project_path is None or Path(plugin.project_path).resolve() == root


def pick_scoped(pid: str, entries: list[EnabledPlugin]) -> EnabledPlugin:
    """The single :class:`EnabledPlugin` a plugin id resolves to across its scope-layered roster entries.

    Entries sharing an install path collapse. Across differing install paths the highest-precedence scope
    wins (``local`` > ``project`` > ``user``), mirroring Claude Code's own settings layering. Two distinct
    install paths at the *same* scope of the *same* project is a corrupt roster and raises
    :class:`PluginListError`.
    """
    roots_by_scope: dict[str | None, set[str]] = {}
    for plugin in entries:
        roots_by_scope.setdefault(plugin.scope, set()).add(plugin.root)
    for scope, roots in roots_by_scope.items():
        if len(roots) > 1:
            raise PluginListError(f"plugin {pid!r} has {len(roots)} install paths at scope {scope!r}: {sorted(roots)}")
    return min(entries, key=lambda p: scope_rank(p.scope))


def dedupe_scoped_roster(parsed: list[EnabledPlugin], root: Path) -> tuple[EnabledPlugin, ...]:
    """Collapse Claude Code's machine-wide roster to one :class:`EnabledPlugin` per plugin id for ``root``.

    Entries belonging to another project are dropped by :func:`governs`; Claude re-lists what remains
    once per scope (``local``/``project``/``user``) it resolves through, so grouping by id and resolving
    each group with :func:`pick_scoped` yields one deterministic entry per id.
    """
    resolved = root.resolve()
    by_id: dict[str, list[EnabledPlugin]] = {}
    for plugin in parsed:
        if governs(plugin, resolved):
            by_id.setdefault(plugin.id, []).append(plugin)
    return tuple(pick_scoped(pid, entries) for pid, entries in by_id.items())


def list_plugins_cli(root: Path, executable: str) -> tuple[EnabledPlugin, ...]:
    """Run ``claude plugin list --json`` in ``root`` and return its enabled, installed plugins.

    The single subprocess boundary of discovery. Keeps only entries Claude Code reports as ``enabled``
    with a string ``installPath``. Every failure is one :class:`PluginListError` — a nonzero exit, a
    timeout, an ``OSError`` that keeps the CLI from executing (a ``claude`` on PATH with a dead shebang
    passes ``shutil.which`` but fails ``exec``), and any roster shape it cannot use: a non-array top
    level, unparseable JSON, or an entry that trips the parser.
    """
    try:
        result = subprocess.run(
            [executable, "plugin", "list", "--json"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise RosterUnavailable(f"claude plugin list timed out after {CLI_TIMEOUT_SECONDS}s") from e
    except OSError as e:
        raise RosterUnavailable(f"claude plugin list could not be executed: {e}") from e
    if result.returncode != 0:
        raise PluginListError(f"claude plugin list exited {result.returncode}: {result.stderr.strip()}")
    try:
        roster = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise PluginListError(f"claude plugin list returned unparseable JSON: {e}") from e
    if not isinstance(roster, list):
        raise PluginListError(f"claude plugin list returned {type(roster).__name__}, expected a JSON array")
    try:
        parsed = [plugin for entry in roster if (plugin := parse_plugin_entry(entry)) is not None]
    except (TypeError, AttributeError, KeyError) as e:
        raise PluginListError(f"claude plugin list returned an unusable roster shape: {e!r}") from e
    # The roster is machine-wide and scope-layered; scope it to this root and collapse to one per id.
    return dedupe_scoped_roster(parsed, root)


def enabled_plugins(root: Path) -> tuple[EnabledPlugin, ...]:
    """The plugins Claude Code has enabled for ``root``, cached per project with stat invalidation.

    Returns ``()`` without spawning the CLI only when Claude Code has no ``installed_plugins.json`` —
    the existence gate that keeps discovery, and every test, hermetic, and the one state where "no
    plugins" is the truth. A fresh snapshot is served directly; otherwise the CLI re-runs once under a
    file lock with a re-stat double-check inside it, so a burst of concurrent events pays a single
    spawn.

    Raises :class:`PluginListError` when the roster cannot be read at all, ``claude`` missing from a
    daemon's minimal ``PATH`` included, and never caches that as a roster: an unreadable roster cached
    as an empty one would make a broken pack layer indistinguishable from a project that enables no
    packs, which is how every plugin-shipped pack on a machine went dead behind one warning line. The
    failure is instead recorded as a :class:`RosterFailure` and re-raised from for
    :data:`FAILURE_TTL`, which keeps that distinction while bounding what a broken roster costs — the
    CLI spawns Node, and re-running it per event is what lets a machine out of fork capacity fail the
    spawn, cache nothing, and immediately spawn again.

    The spawn itself runs under :func:`roster_gate`, which staggers the roots a machine-wide
    invalidation would otherwise stampede, and a spawn that fails outright is recorded as a
    :class:`RosterOutage` so the roots queued behind the gate are told rather than each re-spawning.
    Only that inability is shared: enablement resolves against each project's settings stack, so a
    root's roster is never served to another.
    """
    if not installed_plugins_path().is_file():
        return ()
    records = stat_records(root)
    path, failure = snapshot_path(root), failure_path(root)
    if (snap := PluginSnapshot.load(path)) and snap.fresh(records):
        return snap.plugins
    if (recorded := RosterFailure.load(failure)) and recorded.fresh(records, datetime.now(UTC)):
        raise PluginListError(recorded.message)
    if (outage := RosterOutage.load(outage_path())) and outage.fresh(datetime.now(UTC)):
        raise RosterUnavailable(outage.message)
    if (executable := which("claude")) is None:
        raise PluginListError("claude is not on PATH, so the plugin roster cannot be enumerated")
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path.with_name(path.name + ".lock"))):
        records = stat_records(root)
        if (snap := PluginSnapshot.load(path)) and snap.fresh(records):
            return snap.plugins
        if (recorded := RosterFailure.load(failure)) and recorded.fresh(records, datetime.now(UTC)):
            raise PluginListError(recorded.message)
        with roster_gate():
            if (outage := RosterOutage.load(outage_path())) and outage.fresh(datetime.now(UTC)):
                raise RosterUnavailable(outage.message)
            try:
                plugins = list_plugins_cli(root, executable)
            except RosterUnavailable as exc:
                RosterOutage(at=datetime.now(UTC), message=str(exc)).write(outage_path())
                RosterFailure(stat=records, at=datetime.now(UTC), message=str(exc)).write(failure)
                raise
            except PluginListError as exc:
                RosterFailure(stat=records, at=datetime.now(UTC), message=str(exc)).write(failure)
                raise
            PluginSnapshot(stat=records, plugins=plugins).write(path)
            failure.unlink(missing_ok=True)
            outage_path().unlink(missing_ok=True)
            return plugins


def plugin_pack_root(plugin: EnabledPlugin) -> Path:
    """The fixed ``capt-hook/`` dir under a plugin's install root — present iff the plugin ships a pack."""
    return Path(plugin.root) / manager.PLUGIN_PACK_DIRNAME


def has_plugin_pack(plugin: EnabledPlugin) -> bool:
    return plugin_pack_root(plugin).is_dir()


def plugin_repository(plugin: EnabledPlugin) -> str | None:
    """The ``repository`` url from a plugin's ``.claude-plugin/plugin.json``, or ``None``.

    Routing-only (a hook-misfire fix PR's destination), never a load gate, so an absent or unreadable
    plugin.json is fail-soft — a missing repository just leaves a misfire repo-local.
    """
    path = Path(plugin.root) / ".claude-plugin" / "plugin.json"
    if not path.is_file():
        return None
    try:
        repository = json.loads(path.read_text()).get("repository")
    except (json.JSONDecodeError, OSError):
        return None
    return repository if isinstance(repository, str) and repository else None


def resolve_plugin_pack(plugin: EnabledPlugin) -> manager.ResolvedPack:
    """Load the pack at ``<plugin.root>/capt-hook/{pack.toml, hooks/}``.

    All-or-nothing: a ``capt-hook/`` dir missing its ``pack.toml`` or ``hooks/`` raises
    :class:`~captain_hook.packs.manager.PackError`, never silently dropping the plugin's guards.
    """
    pack_root = plugin_pack_root(plugin)
    descriptor = pack_root / manager.PACK_DESCRIPTOR
    hooks = pack_root / manager.HOOKS_DIRNAME
    if not descriptor.is_file():
        raise manager.PackError(
            f"plugin {plugin.id!r} ships {manager.PLUGIN_PACK_DIRNAME}/ without a {manager.PACK_DESCRIPTOR}"
        )
    if not hooks.is_dir():
        raise manager.PackError(
            f"plugin {plugin.id!r} ships {manager.PLUGIN_PACK_DIRNAME}/ without a {manager.HOOKS_DIRNAME}/ dir"
        )
    return manager.ResolvedPack(
        manager.PluginPack(plugin_id=plugin.id, root=plugin.root, repository=plugin_repository(plugin)),
        hooks,
        manager.PackDescriptor.load(descriptor),
    )


def resolve_plugin_packs(root: Path) -> list[manager.ResolvedPack]:
    """Resolve the pack each of ``root``'s enabled plugins ships, in plugin-id order.

    A plugin with no ``capt-hook/`` dir merely consumes packs and is skipped. A plugin advertising a
    malformed one raises (all-or-nothing). The roster is deduped by full plugin id in
    :func:`list_plugins_cli`, so one plugin id yields one pack.
    """
    return [
        resolve_plugin_pack(plugin)
        for plugin in sorted(enabled_plugins(root), key=lambda p: p.id)
        if has_plugin_pack(plugin)
    ]
