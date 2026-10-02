"""Restore any captain-hook plugin launcher whose ``bin/hook`` differs from what its version shipped.

Every live session runs the ``bin/hook`` of the plugin version it started on, so an agent that rewrites
that file — to quiet hooks the sanctioned :mod:`captain_hook.pause` would have quieted — turns every hook
of every session pinned there into a silent no-op while Claude Code still reports each one a success.
The check covers each install ``installed_plugins.json`` names under the captain-hook cache and each
version dir a live session still has registered in ``.in_use``.

What a version shipped is read from git, never inferred: the install's ``gitCommitSha``, or the
``v<version>`` release tag for a version dir the roster no longer names. A private bare mirror under the
update state dir fetches each ref once, shallow and without blobs, and the launcher's mode and blob id
are cached in :data:`SHIPPED_RECORD`, so a check that finds nothing to repair runs no git at all. A
repair fetches the one blob it needs and swaps it in atomically, keeping a shipped symlink a symlink.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from captain_hook import faults
from captain_hook.review.pipeline import SPAWNED_ENV
from captain_hook.update.updater import breadcrumb, claim, spawn, update_dir, version_tuple
from captain_hook.util import reqenv
from captain_hook.util.fs import atomic_write, read_json
from captain_hook.util.paths import resolve_claude_config_dir

PLUGIN = "captain-hook"
ROSTER_ID = f"{PLUGIN}@{PLUGIN}"
REMOTE = "https://github.com/yasyf/captain-hook.git"
LAUNCHER = "bin/hook"
SOURCE_DIR = "captain_hook"
SHIPPED_RECORD = "launchers.json"
MIRROR = "launchers.git"
CHECK_STAMP = "launchers.stamp"
CHECK_INTERVAL = timedelta(minutes=10)
GIT_TIMEOUT = 120.0
SYMLINK_MODE = "120000"
VERSION = re.compile(r"\d+(?:\.\d+)+")


class LauncherTampered(Exception):
    """A plugin launcher no longer matched what its version shipped and was restored."""


@dataclass(frozen=True, slots=True)
class Shipped:
    mode: str
    oid: str


def cache_root() -> Path:
    return resolve_claude_config_dir() / "plugins" / "cache" / PLUGIN / PLUGIN


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def in_use(version: Path) -> bool:
    return (marks := version / ".in_use").is_dir() and any(
        alive(int(mark.name)) for mark in marks.iterdir() if mark.name.isdigit()
    )


def release_ref(version: Path) -> str:
    return f"refs/tags/v{version.name}"


def candidates() -> dict[Path, str]:
    root = cache_root()
    live = {
        version: release_ref(version)
        for version in (root.iterdir() if root.is_dir() else ())
        if VERSION.fullmatch(version.name) and in_use(version)
    }
    roster = read_json(resolve_claude_config_dir() / "plugins" / "installed_plugins.json", {})
    installed = {
        path: entry.get("gitCommitSha") or release_ref(path)
        for entry in roster.get("plugins", {}).get(ROSTER_ID, [])
        if (path := Path(entry["installPath"])).parent == root and VERSION.fullmatch(path.name)
    }
    return {path: ref for path, ref in (live | installed).items() if (path / "bin").is_dir()}


def blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def on_disk(launcher: Path) -> Shipped | None:
    try:
        info = launcher.lstat()
        if stat.S_ISLNK(info.st_mode):
            return Shipped(SYMLINK_MODE, blob_id(os.fsencode(os.readlink(launcher))))
        return Shipped("100755" if info.st_mode & stat.S_IXUSR else "100644", blob_id(launcher.read_bytes()))
    except FileNotFoundError:
        return None


def run_git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], capture_output=True, timeout=GIT_TIMEOUT, check=True)


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return run_git("-C", str(mirror()), *args)


def mirror() -> Path:
    if not ((path := update_dir() / MIRROR) / "HEAD").exists():
        run_git("init", "--bare", "-q", str(path))
        run_git("-C", str(path), "remote", "add", "origin", REMOTE)
    return path


def resolve_shipped(ref: str) -> Shipped:
    spec = f"+{ref}:{ref}" if ref.startswith("refs/") else ref
    git("fetch", "-q", "--depth=1", "--filter=blob:none", "origin", spec)
    mode, _, oid = git("ls-tree", ref, f"{SOURCE_DIR}/{LAUNCHER}").stdout.decode().split("\t")[0].split(" ")
    return Shipped(mode, oid)


def shipped_for(refs: set[str]) -> dict[str, Shipped]:
    record = update_dir() / SHIPPED_RECORD
    known = {ref: Shipped(*value) for ref, value in read_json(record, {}).items()}
    if missing := refs - known.keys():
        for ref in sorted(missing):
            try:
                known[ref] = resolve_shipped(ref)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                breadcrumb(f"launcher check: cannot resolve what {ref} shipped: {exc}")
        atomic_write(record, json.dumps({ref: [s.mode, s.oid] for ref, s in known.items()}))
    return known


def restore(launcher: Path, shipped: Shipped) -> None:
    content = git("cat-file", "blob", shipped.oid).stdout
    staged = launcher.with_name(f".{launcher.name}.restore-{os.getpid()}")
    staged.unlink(missing_ok=True)
    if shipped.mode == SYMLINK_MODE:
        os.symlink(os.fsdecode(content), staged)
    else:
        staged.write_bytes(content)
        staged.chmod(0o755 if shipped.mode == "100755" else 0o644)
    os.replace(staged, launcher)


def describe(found: Shipped | None) -> str:
    match found:
        case None:
            return "missing"
        case Shipped(mode=mode, oid=oid):
            return f"{'symlink' if mode == SYMLINK_MODE else 'file'} {oid[:12]}"


def check_launchers() -> list[str]:
    """Restore every checked ``bin/hook`` that differs from what its version shipped; the versions restored."""
    targets = candidates()
    shipped = shipped_for(set(targets.values()))
    restored: list[str] = []
    for version, ref in sorted(targets.items(), key=lambda target: version_tuple(target[0].name)):
        if (expected := shipped.get(ref)) is None or (found := on_disk(launcher := version / LAUNCHER)) == expected:
            continue
        try:
            restore(launcher, expected)
        except (OSError, subprocess.SubprocessError) as exc:
            breadcrumb(f"launcher restore failed: {version.name} ({describe(found)}): {exc}")
            continue
        breadcrumb(f"launcher restored: {version.name} {LAUNCHER} was {describe(found)}, shipped {describe(expected)}")
        restored.append(version.name)
    if restored:
        faults.record(
            "plugin launcher",
            LauncherTampered(
                f"restored the shipped {LAUNCHER} of captain-hook {', '.join(restored)}, which had been rewritten "
                "and silenced every hook of the sessions pinned there. Quiet hooks only through "
                "`capt-hook pause --for <duration>`; never edit the plugin cache."
            ),
        )
    return restored


def dispatch_launcher_check() -> None:
    """Async SessionStart entry: at most once per :data:`CHECK_INTERVAL`, detach ``capt-hook update launchers``."""
    if reqenv.getenv(SPAWNED_ENV) or not claim(CHECK_STAMP, CHECK_INTERVAL):
        return
    spawn(["launchers"])
