from __future__ import annotations

import functools
import hashlib
import re
import shutil
import zipfile
from collections.abc import Callable, Iterable
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Any

from filelock import FileLock

from captain_hook.util import http
from captain_hook.util.paths import resolve_cache_home

MODEL_NAME = "en_core_web_sm"
WN_LEXICON = "oewn"
WN_VERSION = "2025+"
WN_SPEC = f"{WN_LEXICON}:{WN_VERSION}"
WN_ARCHIVE_NAME = "english-wordnet-2025-plus.xml.gz"
WN_ASSET_URL = (
    "https://github.com/globalwordnet/english-wordnet/releases/download/2025-edition/english-wordnet-2025-plus.xml.gz"
)
WN_ARCHIVE_SIZE = 12_925_887
WN_ARCHIVE_SHA256 = "31f4af16c54b532fd5484d4cc33aee588a31bb5b70683ae8197842fde5b586bc"
WHEEL_CHECKSUM = re.compile(r"Checksum \.whl:\*\*\s*`([0-9a-f]{64})`")


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
    if not (match := WHEEL_CHECKSUM.search(body)):
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


def ensure_wn_lexicon() -> None:
    import wn

    if wn.lexicons(lexicon=WN_SPEC):
        return
    data_dir = Path(wn.config.data_directory)
    data_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(data_dir / f"{WN_SPEC.replace(':', '-')}.lock")):
        if wn.lexicons(lexicon=WN_SPEC):
            return
        wn.add(fetch_wn_archive(data_dir), progress_handler=None)


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
