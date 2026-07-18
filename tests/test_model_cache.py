from __future__ import annotations

import contextlib
import hashlib
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from captain_hook.util import model_cache

MODEL_VERSION = "3.9.5"


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    yield tmp_path / "spacy" / "models"


@pytest.fixture
def fake_wheel_bytes() -> bytes:
    return b"fake-wheel-content"


@pytest.fixture
def pinned_version(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(model_cache, "spacy_minor", lambda: "3.9")
    monkeypatch.setattr(model_cache, "model_version", lambda: MODEL_VERSION)
    return MODEL_VERSION


@pytest.fixture
def pinned_sha(fake_wheel_bytes: bytes, monkeypatch: pytest.MonkeyPatch) -> str:
    digest = hashlib.sha256(fake_wheel_bytes).hexdigest()
    monkeypatch.setattr(model_cache, "model_sha256", lambda _version: digest)
    return digest


@pytest.fixture
def download_spy(
    monkeypatch: pytest.MonkeyPatch,
    fake_wheel_bytes: bytes,
) -> MagicMock:
    spy = MagicMock()

    def fake_download(url: str, dest: Path) -> None:
        spy(url)
        dest.write_bytes(fake_wheel_bytes)

    monkeypatch.setattr(model_cache.http, "github_download", fake_download)
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
            pipeline = Path(target) / model_cache.MODEL_NAME / f"{model_cache.MODEL_NAME}-{MODEL_VERSION}"
            pipeline.mkdir(parents=True, exist_ok=True)
            (pipeline / "config.cfg").write_text("[paths]\n")

    monkeypatch.setattr(model_cache.zipfile, "ZipFile", FakeZip)
    return spy


def seed_cache(cache_dir: Path, version: str, sentinel: str | None = "aa" * 32) -> Path:
    extract = cache_dir / f"{model_cache.MODEL_NAME}-{version}"
    pipeline = extract / model_cache.MODEL_NAME / f"{model_cache.MODEL_NAME}-{version}"
    pipeline.mkdir(parents=True)
    if sentinel is not None:
        (extract / ".sha256").write_text(sentinel)
    return pipeline


def test_downloads_when_cache_empty(
    cache_dir: Path,
    pinned_version: str,
    pinned_sha: str,
    download_spy: MagicMock,
    fake_zipfile: MagicMock,
) -> None:
    path = model_cache.ensure_spacy_model()

    assert download_spy.call_count == 1
    assert fake_zipfile.call_count == 1
    assert path.exists()
    assert (
        path
        == cache_dir
        / f"{model_cache.MODEL_NAME}-{MODEL_VERSION}"
        / model_cache.MODEL_NAME
        / f"{model_cache.MODEL_NAME}-{MODEL_VERSION}"
    )
    sentinel = cache_dir / f"{model_cache.MODEL_NAME}-{MODEL_VERSION}" / ".sha256"
    assert sentinel.read_text() == pinned_sha


def test_cached_model_skips_all_network(
    cache_dir: Path,
    pinned_version: str,
    download_spy: MagicMock,
    fake_zipfile: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = seed_cache(cache_dir, MODEL_VERSION)

    def no_network() -> str:
        raise AssertionError("model_version must not be resolved when the cache is warm")

    monkeypatch.setattr(model_cache, "model_version", no_network)

    path = model_cache.ensure_spacy_model()

    download_spy.assert_not_called()
    fake_zipfile.assert_not_called()
    assert path == pipeline


def test_prefers_newest_cached_patch_for_minor(
    cache_dir: Path,
    pinned_version: str,
) -> None:
    seed_cache(cache_dir, "3.9.2")
    newest = seed_cache(cache_dir, "3.9.10")
    seed_cache(cache_dir, "3.8.0")

    assert model_cache.cached_pipeline() == newest


def test_ignores_cache_from_other_spacy_minor(
    cache_dir: Path,
    pinned_version: str,
    pinned_sha: str,
    download_spy: MagicMock,
    fake_zipfile: MagicMock,
) -> None:
    seed_cache(cache_dir, "3.8.0")

    path = model_cache.ensure_spacy_model()

    assert download_spy.call_count == 1
    assert (
        path
        == cache_dir
        / f"{model_cache.MODEL_NAME}-{MODEL_VERSION}"
        / model_cache.MODEL_NAME
        / f"{model_cache.MODEL_NAME}-{MODEL_VERSION}"
    )


def test_redownloads_when_sentinel_missing(
    cache_dir: Path,
    pinned_version: str,
    pinned_sha: str,
    download_spy: MagicMock,
    fake_zipfile: MagicMock,
) -> None:
    pipeline = seed_cache(cache_dir, MODEL_VERSION, sentinel=None)

    path = model_cache.ensure_spacy_model()

    assert download_spy.call_count == 1
    assert fake_zipfile.call_count == 1
    assert (cache_dir / f"{model_cache.MODEL_NAME}-{MODEL_VERSION}" / ".sha256").read_text() == pinned_sha
    assert path == pipeline


def test_raises_on_post_download_digest_mismatch(
    cache_dir: Path,
    pinned_version: str,
    download_spy: MagicMock,
    fake_zipfile: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_cache, "model_sha256", lambda _version: "ff" * 32)

    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        model_cache.ensure_spacy_model()


def test_model_version_resolves_from_compatibility_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_cache, "spacy_minor", lambda: "3.9")
    monkeypatch.setattr(
        model_cache,
        "fetch_json",
        lambda _url: {"spacy": {"3.9": {model_cache.MODEL_NAME: ["3.9.5", "3.9.4"]}}},
    )

    assert model_cache.model_version() == "3.9.5"


def test_model_sha256_parses_release_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "ab" * 32
    body = f"> **Checksum .tar.gz:** `{'cd' * 32}`<br />**Checksum .whl:** `{sha}`"
    monkeypatch.setattr(model_cache, "fetch_json", lambda _url: {"body": body})

    assert model_cache.model_sha256("3.9.5") == sha


def test_model_sha256_raises_when_checksum_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_cache, "fetch_json", lambda _url: {"body": "no checksums here"})

    with pytest.raises(RuntimeError, match="no wheel checksum"):
        model_cache.model_sha256("3.9.5")


class FakeWn:
    def __init__(self, data_dir: Path, *, installed: bool) -> None:
        self.config = SimpleNamespace(
            data_directory=str(data_dir),
            get_project_info=lambda lexicon: {"version": "2025+"} if lexicon == model_cache.WN_LEXICON else None,
        )
        self.installed = installed
        self.downloads: list[str] = []

    def lexicons(self, lexicon: str) -> list[str]:
        assert lexicon == f"{model_cache.WN_LEXICON}:2025+"
        return ["oewn"] if self.installed else []

    def download(self, spec: str, progress_handler: object = None) -> None:
        self.downloads.append(spec)
        self.installed = True


@pytest.fixture
def fake_wn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeWn:
    fake = FakeWn(tmp_path / "wn-data", installed=False)
    monkeypatch.setitem(sys.modules, "wn", fake)
    return fake


def test_wn_lexicon_cached_skips_download(fake_wn: FakeWn) -> None:
    fake_wn.installed = True

    model_cache.ensure_wn_lexicon()

    assert fake_wn.downloads == []


def test_wn_lexicon_downloads_once_under_filelock(
    tmp_path: Path, fake_wn: FakeWn, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks: list[str] = []
    monkeypatch.setattr(model_cache, "FileLock", lambda path: (locks.append(path), contextlib.nullcontext())[1])

    model_cache.ensure_wn_lexicon()
    model_cache.ensure_wn_lexicon()

    assert fake_wn.downloads == [f"{model_cache.WN_LEXICON}:2025+"]
    assert locks == [str(tmp_path / "wn-data" / "oewn-2025+.lock")]


def test_ensure_nlp_resources_composes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(model_cache, "ensure_spacy_model", lambda: calls.append("spacy"))
    monkeypatch.setattr(model_cache, "ensure_wn_lexicon", lambda: calls.append("wn"))

    model_cache.ensure_nlp_resources()

    assert calls == ["spacy", "wn"]
