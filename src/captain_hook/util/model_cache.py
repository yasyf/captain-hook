from __future__ import annotations

import functools
import hashlib
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

from filelock import FileLock

MODEL_NAME = "en_core_web_sm"
MODEL_VERSION = "3.7.1"
MODEL_URL = (
    f"https://github.com/explosion/spacy-models/releases/download/"
    f"{MODEL_NAME}-{MODEL_VERSION}/{MODEL_NAME}-{MODEL_VERSION}-py3-none-any.whl"
)
MODEL_SHA256 = "86cc141f63942d4b2c5fcee06630fd6f904788d2f0ab005cce45aadb8fb73889"


@functools.cache
def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "spacy" / "models"


def ensure_spacy_model() -> Path:
    extract = cache_root() / f"{MODEL_NAME}-{MODEL_VERSION}"
    pipeline = extract / MODEL_NAME / f"{MODEL_NAME}-{MODEL_VERSION}"
    sentinel = extract / ".sha256"
    if pipeline.is_dir() and sentinel.is_file() and sentinel.read_text().strip() == MODEL_SHA256:
        return pipeline
    extract.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(extract.with_suffix(".lock"))):
        if pipeline.is_dir() and sentinel.is_file() and sentinel.read_text().strip() == MODEL_SHA256:
            return pipeline
        wheel, _ = urllib.request.urlretrieve(MODEL_URL)
        if (digest := hashlib.sha256(Path(wheel).read_bytes()).hexdigest()) != MODEL_SHA256:
            raise RuntimeError(f"sha256 mismatch for {MODEL_URL}: got {digest}, expected {MODEL_SHA256}")
        if extract.exists():
            shutil.rmtree(extract)
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(extract)
        sentinel.write_text(MODEL_SHA256)
    return pipeline
