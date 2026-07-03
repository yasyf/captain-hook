from __future__ import annotations

import functools
import hashlib
import re
import shutil
import zipfile
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Any

from filelock import FileLock

from captain_hook.settings import resolve_cache_home
from captain_hook.util import http

MODEL_NAME = "en_core_web_sm"
WN_LEXICON = "oewn:2025"
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


def ensure_wn_lexicon() -> None:
    import wn

    if wn.lexicons(lexicon=WN_LEXICON):
        return
    data_dir = Path(wn.config.data_directory)
    data_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(data_dir / f"{WN_LEXICON.replace(':', '-')}.lock")):
        if wn.lexicons(lexicon=WN_LEXICON):
            return
        wn.download(WN_LEXICON, progress_handler=None)


def ensure_nlp_resources() -> None:
    """Provision the NLP resources hooks need: the pinned spaCy pipeline (~13MB) and the oewn:2025 wn lexicon (~231MB).

    Idempotent and cheap once cached; downloads are filelock-guarded so concurrent
    sessions never race a fetch.
    """
    ensure_spacy_model()
    ensure_wn_lexicon()
