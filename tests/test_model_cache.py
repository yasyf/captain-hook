from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from captain_hook.util import model_cache


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    model_cache.cache_root.cache_clear()
    return tmp_path / "spacy" / "models"


@pytest.fixture
def fake_wheel_bytes() -> bytes:
    return b"fake-wheel-content"


@pytest.fixture
def pinned_sha(fake_wheel_bytes: bytes, monkeypatch: pytest.MonkeyPatch) -> str:
    digest = hashlib.sha256(fake_wheel_bytes).hexdigest()
    monkeypatch.setattr(model_cache, "MODEL_SHA256", digest)
    return digest


@pytest.fixture
def urlretrieve_spy(
    monkeypatch: pytest.MonkeyPatch,
    fake_wheel_bytes: bytes,
    tmp_path: Path,
) -> MagicMock:
    spy = MagicMock()

    def fake_urlretrieve(url: str, *_args: object, **_kwargs: object) -> tuple[str, None]:
        spy(url)
        wheel = tmp_path / "fetched.whl"
        wheel.write_bytes(fake_wheel_bytes)
        return str(wheel), None

    monkeypatch.setattr(model_cache.urllib.request, "urlretrieve", fake_urlretrieve)
    monkeypatch.setattr(model_cache, "FileLock", lambda _path: contextlib.nullcontext())
    return spy


@pytest.fixture
def fake_zipfile(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    spy = MagicMock()

    class FakeZip:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeZip:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def extractall(self, target: str | Path) -> None:
            spy(target)
            pipeline = Path(target) / model_cache.MODEL_NAME / f"{model_cache.MODEL_NAME}-{model_cache.MODEL_VERSION}"
            pipeline.mkdir(parents=True, exist_ok=True)
            (pipeline / "config.cfg").write_text("[paths]\n")

    monkeypatch.setattr(model_cache.zipfile, "ZipFile", FakeZip)
    return spy


def test_downloads_when_cache_empty(
    cache_dir: Path,
    pinned_sha: str,
    urlretrieve_spy: MagicMock,
    fake_zipfile: MagicMock,
) -> None:
    path = model_cache.ensure_spacy_model()

    assert urlretrieve_spy.call_count == 1
    assert fake_zipfile.call_count == 1
    assert path.exists()
    assert path == cache_dir / f"{model_cache.MODEL_NAME}-{model_cache.MODEL_VERSION}" / model_cache.MODEL_NAME / f"{model_cache.MODEL_NAME}-{model_cache.MODEL_VERSION}"
    sentinel = cache_dir / f"{model_cache.MODEL_NAME}-{model_cache.MODEL_VERSION}" / ".sha256"
    assert sentinel.read_text() == pinned_sha


def test_skips_download_when_sentinel_matches(
    cache_dir: Path,
    pinned_sha: str,
    urlretrieve_spy: MagicMock,
    fake_zipfile: MagicMock,
) -> None:
    extract = cache_dir / f"{model_cache.MODEL_NAME}-{model_cache.MODEL_VERSION}"
    pipeline = extract / model_cache.MODEL_NAME / f"{model_cache.MODEL_NAME}-{model_cache.MODEL_VERSION}"
    pipeline.mkdir(parents=True)
    (extract / ".sha256").write_text(pinned_sha)

    path = model_cache.ensure_spacy_model()

    urlretrieve_spy.assert_not_called()
    fake_zipfile.assert_not_called()
    assert path == pipeline


def test_redownloads_on_sha_mismatch(
    cache_dir: Path,
    pinned_sha: str,
    urlretrieve_spy: MagicMock,
    fake_zipfile: MagicMock,
) -> None:
    extract = cache_dir / f"{model_cache.MODEL_NAME}-{model_cache.MODEL_VERSION}"
    pipeline = extract / model_cache.MODEL_NAME / f"{model_cache.MODEL_NAME}-{model_cache.MODEL_VERSION}"
    pipeline.mkdir(parents=True)
    (extract / ".sha256").write_text("00" * 32)

    path = model_cache.ensure_spacy_model()

    assert urlretrieve_spy.call_count == 1
    assert fake_zipfile.call_count == 1
    assert (extract / ".sha256").read_text() == pinned_sha
    assert path == pipeline


def test_raises_on_post_download_digest_mismatch(
    cache_dir: Path,
    urlretrieve_spy: MagicMock,
    fake_zipfile: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_cache, "MODEL_SHA256", "ff" * 32)

    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        model_cache.ensure_spacy_model()
