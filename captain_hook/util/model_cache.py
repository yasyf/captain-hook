from __future__ import annotations

import functools
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from contextlib import closing
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Any

from filelock import FileLock

from captain_hook.util import http
from captain_hook.util.paths import resolve_cache_home

MODEL_NAME = "en_core_web_sm"
WN_SPEC = "oewn:2025+"
WN_ARCHIVE_NAME = "english-wordnet-2025-plus.xml.gz"
WN_ASSET_URL = (
    "https://github.com/globalwordnet/english-wordnet/releases/download/2025-edition/english-wordnet-2025-plus.xml.gz"
)
WN_ARCHIVE_SIZE = 12_925_887
WN_ARCHIVE_SHA256 = "31f4af16c54b532fd5484d4cc33aee588a31bb5b70683ae8197842fde5b586bc"
WN_IMPORT = """
import sys

import wn

wn.config.data_directory = sys.argv[1]
for stale in sys.argv[3:]:
    wn.remove(stale, progress_handler=None)
wn.add(sys.argv[2], progress_handler=None)
"""
WN_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


def cache_root() -> Path:
    return resolve_cache_home() / "spacy" / "models"


@functools.cache
def spacy_minor() -> str:
    major, minor, *_ = installed_version("spacy").split(".")
    return f"{major}.{minor}"


def fetch_json(url: str) -> Any:
    return http.github_get_json(url)


@functools.cache
def model_version() -> str:
    return fetch_json("https://raw.githubusercontent.com/explosion/spacy-models/master/compatibility.json")["spacy"][
        spacy_minor()
    ][MODEL_NAME][0]


@functools.cache
def model_sha256(version: str) -> str:
    body = fetch_json(f"https://api.github.com/repos/explosion/spacy-models/releases/tags/{MODEL_NAME}-{version}")[
        "body"
    ]
    if not (match := re.search(r"Checksum \.whl:\*\*\s*`([0-9a-f]{64})`", body)):
        raise RuntimeError(f"no wheel checksum in release notes for {MODEL_NAME}-{version}")
    return match.group(1)


def version_key(dirname: str) -> tuple[int, ...]:
    return tuple(int(part) for part in dirname.removeprefix(f"{MODEL_NAME}-").split("."))


def cached_pipeline() -> Path | None:
    extracts = (d for d in cache_root().glob(f"{MODEL_NAME}-{spacy_minor()}.*") if d.is_dir())
    for extract in sorted(extracts, key=lambda d: version_key(d.name), reverse=True):
        pipeline = extract / MODEL_NAME / extract.name
        if pipeline.is_dir() and (extract / ".sha256").is_file():
            return pipeline
    return None


def ensure_spacy_model() -> Path:
    if cached := cached_pipeline():
        return cached
    version = model_version()
    expected = model_sha256(version)
    extract = cache_root() / f"{MODEL_NAME}-{version}"
    extract.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(extract.with_suffix(".lock"))):
        if cached := cached_pipeline():
            return cached
        wheel = extract.parent / f"{extract.name}.whl"
        http.github_download(
            f"https://github.com/explosion/spacy-models/releases/download/"
            f"{MODEL_NAME}-{version}/{MODEL_NAME}-{version}-py3-none-any.whl",
            wheel,
        )
        if (digest := hashlib.sha256(wheel.read_bytes()).hexdigest()) != expected:
            raise RuntimeError(f"sha256 mismatch for {MODEL_NAME}-{version}: got {digest}, expected {expected}")
        if extract.exists():
            shutil.rmtree(extract)
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(extract)
        wheel.unlink()
        (extract / ".sha256").write_text(expected)
    return extract / MODEL_NAME / extract.name


def wn_archive_matches(path: Path) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == WN_ARCHIVE_SIZE
        and hashlib.sha256(path.read_bytes()).hexdigest() == WN_ARCHIVE_SHA256
    )


def fetch_wn_archive(data_dir: Path) -> Path:
    archive = data_dir / WN_ARCHIVE_NAME
    if wn_archive_matches(archive):
        return archive
    pending = archive.with_name(f".{archive.name}.part")
    try:
        http.github_download(WN_ASSET_URL, pending)
        size = pending.stat().st_size
        digest = hashlib.sha256(pending.read_bytes()).hexdigest()
        if size != WN_ARCHIVE_SIZE or digest != WN_ARCHIVE_SHA256:
            raise RuntimeError(
                f"integrity mismatch for {WN_SPEC}: got {size} bytes sha256 {digest}, "
                f"expected {WN_ARCHIVE_SIZE} bytes sha256 {WN_ARCHIVE_SHA256}"
            )
        pending.replace(archive)
    finally:
        pending.unlink(missing_ok=True)
    return archive


def wn_sidecars(database: Path) -> list[Path]:
    return [database.with_name(f"{database.name}{suffix}") for suffix in WN_SIDECAR_SUFFIXES]


def intact_wn_lexicons(database: Path) -> set[str]:
    if database.is_symlink() or not database.is_file() or any(path.exists() for path in wn_sidecars(database)):
        return set()
    try:
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                return set()
            return {f"{lexicon}:{version}" for lexicon, version in conn.execute("SELECT id, version FROM lexicons")}
    except sqlite3.DatabaseError:
        return set()


def wn_fingerprint(database: Path) -> str:
    stat = database.stat()
    return f"{stat.st_size} {stat.st_mtime_ns}"


def wn_verified(database: Path, stamp: Path) -> bool:
    try:
        return stamp.read_text() == wn_fingerprint(database)
    except FileNotFoundError:
        return False


def build_wn_database(database: Path) -> None:
    # wn.add writes with journal_mode=MEMORY, so a killed import corrupts its file and leaves no
    # journal. Import into a staging copy; rename it into place only after quick_check passes.
    for abandoned in database.parent.glob(f".{database.name}.staging-*"):
        shutil.rmtree(abandoned, ignore_errors=True)
    staging = Path(tempfile.mkdtemp(dir=database.parent, prefix=f".{database.name}.staging-"))
    staged = staging / database.name
    installed = intact_wn_lexicons(database)
    others = installed - {WN_SPEC}
    if others:
        with (
            closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(staged)) as target,
        ):
            source.backup(target)
    archive = fetch_wn_archive(database.parent)
    stale = sorted(installed - others) if others else []
    subprocess.run([sys.executable, "-P", "-c", WN_IMPORT, str(staging), str(archive), *stale], check=True)
    if WN_SPEC not in intact_wn_lexicons(staged):
        raise RuntimeError(f"importing {WN_SPEC} left no intact database at {staged}")
    with staged.open("rb") as built:
        os.fsync(built.fileno())
    if not others:
        for unfinished in (database, *wn_sidecars(database)):
            unfinished.unlink(missing_ok=True)
    staged.replace(database)
    shutil.rmtree(staging)


def ensure_wn_lexicon() -> None:
    import wn

    if sqlite3.threadsafety != 3:
        raise RuntimeError(f"wn multithreading requires serialized sqlite (threadsafety 3), got {sqlite3.threadsafety}")
    # wn pools one process-global sqlite connection; worker dispatch reads it from many
    # threads, which serialized sqlite makes safe.
    wn.config.allow_multithreading = True
    database = Path(wn.config.database_path).absolute()
    stem = WN_SPEC.replace(":", "-")
    stamp = database.with_name(f"{stem}.verified")
    if wn_verified(database, stamp):
        return
    database.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(database.with_name(f"{stem}.lock"))):
        if not wn_verified(database, stamp):
            build_wn_database(database)
            stamp.write_text(wn_fingerprint(database))


def ensure_nlp_resources() -> None:
    """Provision the NLP resources hooks need: the pinned spaCy pipeline and oewn lexicon.

    Idempotent and cheap once cached; downloads are filelock-guarded so concurrent
    sessions never race a fetch.
    """
    ensure_spacy_model()
    ensure_wn_lexicon()


# A pack.toml ``resources`` entry names one of these; the value provisions it. The keys are the
# resource identifiers a pack declares (see the general/steering builtin descriptors).
RESOURCE_PROVISIONERS: dict[str, Callable[[], object]] = {
    "spacy:en_core_web_sm": ensure_spacy_model,
    "wordnet:oewn:2025": ensure_wn_lexicon,
}


def unknown_resources(resources: Iterable[str]) -> list[str]:
    """The declared resource names that no provisioner knows — the validation `pack test` reports."""
    return [name for name in dict.fromkeys(resources) if name not in RESOURCE_PROVISIONERS]


def provision_resources(resources: Iterable[str]) -> None:
    """Provision every declared pack resource, deduped. Crashes on an unknown resource name."""
    if unknown := unknown_resources(resources):
        raise ValueError(f"unknown pack resource(s): {', '.join(unknown)}")
    for name in dict.fromkeys(resources):
        RESOURCE_PROVISIONERS[name]()
