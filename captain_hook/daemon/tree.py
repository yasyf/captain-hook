from __future__ import annotations

import errno
import os
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

TREE_READ_ATTEMPTS = 3

type HookEntry = tuple[str, int, int, int]
type StatStamp = tuple[int, int, int, int, int, int]
type DirectoryStamp = tuple[StatStamp, StatStamp | None]


def stat_stamp(value: os.stat_result) -> StatStamp:
    return value.st_dev, value.st_ino, value.st_mode, value.st_mtime_ns, value.st_ctime_ns, value.st_size


def directory_stamp(path: Path, *, root: bool) -> DirectoryStamp:
    entry = path.lstat()
    target = path.stat() if root and stat.S_ISLNK(entry.st_mode) else None
    if not stat.S_ISDIR((target or entry).st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "hook tree directory changed type", str(path))
    return stat_stamp(entry), stat_stamp(target) if target is not None else None


class TreeChanged(OSError):
    def __init__(self, path: Path) -> None:
        super().__init__(errno.EAGAIN, "hook tree changed during fingerprint", str(path))


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    stamp: DirectoryStamp
    names: tuple[str, ...]


@dataclass(slots=True)
class RootManifest:
    guard: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0
    epoch: int = 0
    manifest_epoch: int = 0
    manifest: dict[Path, DirectoryListing] = field(default_factory=dict)


class DirectoryTreeCache:
    def __init__(self, maxsize: int = 32) -> None:
        if maxsize < 1:
            raise ValueError("directory tree cache must retain at least one root")
        self.maxsize = maxsize
        self._roots: OrderedDict[Path, RootManifest] = OrderedDict()
        self._available = threading.Condition()

    def _acquire(self, path: Path) -> RootManifest:
        with self._available:
            while True:
                if (root := self._roots.get(path)) is not None:
                    self._roots.move_to_end(path)
                    root.users += 1
                    return root
                if len(self._roots) < self.maxsize:
                    root = RootManifest(users=1)
                    self._roots[path] = root
                    return root
                victim = next((key for key, root in self._roots.items() if root.users == 0), None)
                if victim is not None:
                    del self._roots[victim]
                    continue
                self._available.wait()

    def cache_clear(self) -> None:
        with self._available:
            for path, root in tuple(self._roots.items()):
                if root.users:
                    root.epoch += 1
                else:
                    del self._roots[path]
            self._available.notify_all()

    def entries(self, root: Path, *, fresh: bool = False) -> tuple[HookEntry, ...]:
        for _ in range(TREE_READ_ATTEMPTS - 1):
            try:
                return self._entries(root, fresh=fresh)
            except TreeChanged:
                continue
        return self._entries(root, fresh=fresh)

    def _entries(self, root: Path, *, fresh: bool) -> tuple[HookEntry, ...]:
        path = root.absolute()
        held = self._acquire(path)
        try:
            with held.guard:
                with self._available:
                    epoch = held.epoch
                previous = held.manifest if held.manifest_epoch == epoch and not fresh else {}
                manifest: dict[Path, DirectoryListing] = {}
                output: list[HookEntry] = []
                try:
                    self._walk(path, path, directory_stamp(path, root=True), previous, manifest, output)
                    for directory, listing in manifest.items():
                        if directory_stamp(directory, root=directory == path) != listing.stamp:
                            raise TreeChanged(directory)
                    result = tuple(sorted(output))
                    with self._available:
                        if held.epoch != epoch:
                            raise OSError(errno.EAGAIN, "hook tree cache invalidated during fingerprint", str(path))
                        held.manifest = manifest
                        held.manifest_epoch = epoch
                    return result
                except BaseException:
                    held.manifest = {}
                    raise
        finally:
            with self._available:
                held.users -= 1
                self._available.notify_all()

    def _walk(
        self,
        path: Path,
        base: Path,
        stamp: DirectoryStamp,
        previous: dict[Path, DirectoryListing],
        manifest: dict[Path, DirectoryListing],
        output: list[HookEntry],
    ) -> None:
        listing = previous.get(path)
        if listing is None or listing.stamp != stamp:
            with os.scandir(path) as entries:
                names = tuple(sorted(entry.name for entry in entries if entry.name != "__pycache__"))
            if directory_stamp(path, root=path == base) != stamp:
                raise TreeChanged(path)
            listing = DirectoryListing(stamp, names)
        manifest[path] = listing
        for name in listing.names:
            child = path / name
            metadata = child.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                self._walk(child, base, (stat_stamp(metadata), None), previous, manifest, output)
            elif stat.S_ISREG(metadata.st_mode) and not name.endswith((".pyc", ".pyo")):
                output.append(
                    (os.path.relpath(child, base), metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_size)
                )
