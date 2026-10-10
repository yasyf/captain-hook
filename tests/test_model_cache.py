from __future__ import annotations

import contextlib
import gzip
import hashlib
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from captain_hook.util import model_cache

MODEL_VERSION = "3.9.5"
RUN = subprocess.run


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


LEXICON_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE LexicalResource SYSTEM "http://globalwordnet.github.io/schemas/WN-LMF-1.0.dtd">
<LexicalResource xmlns:dc="http://purl.org/dc/elements/1.1/">
  <Lexicon id="{lexicon}" label="{lexicon}" language="en" email="a@example.com"
           license="https://creativecommons.org/licenses/by/4.0/" version="{version}">
    <LexicalEntry id="{lexicon}-change-n">
      <Lemma writtenForm="{lemma}" partOfSpeech="n"/>
      <Sense id="{lexicon}-change-n-1" synset="{lexicon}-1-n"/>
    </LexicalEntry>
    <Synset id="{lexicon}-1-n" ili="" partOfSpeech="n"/>
  </Lexicon>
</LexicalResource>
"""


@dataclass
class WnHome:
    data_dir: Path
    downloads: list[Path] = field(default_factory=list)
    imports: list[list[str]] = field(default_factory=list)
    locks: list[str] = field(default_factory=list)

    @property
    def database(self) -> Path:
        return self.data_dir / "wn.db"

    @property
    def stamp(self) -> Path:
        return self.data_dir / "oewn-2025+.verified"

    def stagings(self) -> list[Path]:
        return list(self.data_dir.glob(".wn.db.staging-*"))

    def lemmas(self) -> set[str]:
        with contextlib.closing(sqlite3.connect(self.database)) as conn:
            return {form for (form,) in conn.execute("SELECT form FROM forms")}

    def seed(self, lexicon: str, version: str, lemma: str = "unfinished") -> None:
        self.data_dir.mkdir(exist_ok=True)
        source = self.data_dir.parent / f"{lexicon}.xml"
        source.write_text(LEXICON_XML.format(lexicon=lexicon, version=version, lemma=lemma))
        RUN([sys.executable, "-P", "-c", model_cache.WN_IMPORT, str(self.data_dir), str(source)], check=True)


@pytest.fixture
def wn_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WnHome:
    import wn

    home = WnHome(tmp_path / "wn-data")
    payload = gzip.compress(LEXICON_XML.format(lexicon="oewn", version="2025+", lemma="change").encode())
    lock = model_cache.FileLock

    def download(_url: str, dest: Path) -> None:
        home.downloads.append(dest)
        dest.write_bytes(payload)

    def import_lexicon(argv: list[str], **kwargs: object) -> object:
        home.imports.append(argv)
        return RUN(argv, **kwargs)

    def take_lock(path: str) -> object:
        home.locks.append(path)
        return lock(path)

    monkeypatch.setattr(wn.config, "data_directory", home.data_dir)
    monkeypatch.setattr(wn.config, "allow_multithreading", False)
    monkeypatch.setattr(model_cache, "WN_ARCHIVE_SIZE", len(payload))
    monkeypatch.setattr(model_cache, "WN_ARCHIVE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(model_cache.http, "github_download", download)
    monkeypatch.setattr(model_cache.subprocess, "run", import_lexicon)
    monkeypatch.setattr(model_cache, "FileLock", take_lock)
    return home


def test_wn_lexicon_source_is_exactly_pinned() -> None:
    assert model_cache.WN_SPEC == "oewn:2025+"
    assert model_cache.WN_ASSET_URL == (
        "https://github.com/globalwordnet/english-wordnet/releases/download/"
        "2025-edition/english-wordnet-2025-plus.xml.gz"
    )
    assert model_cache.WN_ARCHIVE_SIZE == 12_925_887
    assert model_cache.WN_ARCHIVE_SHA256 == "31f4af16c54b532fd5484d4cc33aee588a31bb5b70683ae8197842fde5b586bc"


def test_wn_lexicon_first_use_builds_in_staging_then_verifies_and_stamps(wn_home: WnHome) -> None:
    model_cache.ensure_wn_lexicon()
    model_cache.ensure_wn_lexicon()

    archive = wn_home.data_dir / model_cache.WN_ARCHIVE_NAME
    assert wn_home.downloads == [archive.with_name(f".{archive.name}.part")]
    assert [argv[-1] for argv in wn_home.imports] == [str(archive)]
    assert Path(wn_home.imports[0][-2]).parent == wn_home.data_dir
    assert wn_home.locks == [str(wn_home.data_dir / "oewn-2025+.lock")]
    assert model_cache.intact_wn_lexicons(wn_home.database) == {"oewn:2025+"}
    assert wn_home.stamp.read_text() == model_cache.wn_fingerprint(wn_home.database)
    assert wn_home.stagings() == []


def test_wn_lexicon_concurrent_first_uses_share_one_build(wn_home: WnHome) -> None:
    callers = 4
    barrier = threading.Barrier(callers)
    failures: list[BaseException] = []

    def first_use() -> None:
        barrier.wait()
        try:
            model_cache.ensure_wn_lexicon()
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=first_use) for _ in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(wn_home.imports) == 1
    assert model_cache.intact_wn_lexicons(wn_home.database) == {"oewn:2025+"}


def test_wn_lexicon_recovers_from_an_interrupted_build(wn_home: WnHome) -> None:
    abandoned = wn_home.data_dir / ".wn.db.staging-abandoned"
    abandoned.mkdir(parents=True)
    (abandoned / "wn.db").write_bytes(b"SQLite format 3\x00 half an import")

    model_cache.ensure_wn_lexicon()

    assert len(wn_home.imports) == 1
    assert model_cache.intact_wn_lexicons(wn_home.database) == {"oewn:2025+"}
    assert wn_home.stagings() == []


@pytest.mark.parametrize("sidecar", ["wn.db-journal", "wn.db-wal", "wn.db-shm"])
def test_wn_lexicon_rebuilds_a_database_with_an_unfinished_write(wn_home: WnHome, sidecar: str) -> None:
    wn_home.seed("oewn", "2025+")
    (wn_home.data_dir / sidecar).write_bytes(b"unfinished")
    assert model_cache.intact_wn_lexicons(wn_home.database) == set()

    model_cache.ensure_wn_lexicon()

    assert len(wn_home.imports) == 1
    assert not (wn_home.data_dir / sidecar).exists()
    assert model_cache.intact_wn_lexicons(wn_home.database) == {"oewn:2025+"}


def test_wn_lexicon_rebuilds_a_damaged_database(wn_home: WnHome) -> None:
    wn_home.seed("oewn", "2025+")
    damaged = bytearray(wn_home.database.read_bytes())
    damaged[4096:8192] = b"\xff" * 4096
    wn_home.database.write_bytes(damaged)
    assert model_cache.intact_wn_lexicons(wn_home.database) == set()

    model_cache.ensure_wn_lexicon()

    assert len(wn_home.imports) == 1
    assert model_cache.intact_wn_lexicons(wn_home.database) == {"oewn:2025+"}


def test_wn_lexicon_reverifies_a_database_that_changed_after_its_stamp(wn_home: WnHome) -> None:
    model_cache.ensure_wn_lexicon()
    wn_home.database.write_bytes(b"not a database")

    model_cache.ensure_wn_lexicon()

    assert len(wn_home.imports) == 2
    assert model_cache.intact_wn_lexicons(wn_home.database) == {"oewn:2025+"}
    assert wn_home.stamp.read_text() == model_cache.wn_fingerprint(wn_home.database)


def test_wn_lexicon_rebuilds_a_database_no_stamp_vouches_for(wn_home: WnHome) -> None:
    wn_home.seed("oewn", "2025+")
    assert model_cache.intact_wn_lexicons(wn_home.database) == {"oewn:2025+"}

    model_cache.ensure_wn_lexicon()

    assert len(wn_home.imports) == 1
    assert wn_home.lemmas() == {"change"}
    assert wn_home.stamp.read_text() == model_cache.wn_fingerprint(wn_home.database)


def test_wn_lexicon_keeps_the_other_lexicons_installed(wn_home: WnHome) -> None:
    wn_home.seed("other", "1.0", lemma="kept")

    model_cache.ensure_wn_lexicon()

    assert [argv[-1] for argv in wn_home.imports] == [str(wn_home.data_dir / model_cache.WN_ARCHIVE_NAME)]
    assert model_cache.intact_wn_lexicons(wn_home.database) == {"other:1.0", "oewn:2025+"}
    assert wn_home.lemmas() == {"kept", "change"}


def test_wn_lexicon_replaces_its_unvouched_lexicon_beside_the_others(wn_home: WnHome) -> None:
    wn_home.seed("other", "1.0", lemma="kept")
    wn_home.seed("oewn", "2025+")

    model_cache.ensure_wn_lexicon()

    assert [argv[-1] for argv in wn_home.imports] == ["oewn:2025+"]
    assert model_cache.intact_wn_lexicons(wn_home.database) == {"other:1.0", "oewn:2025+"}
    assert wn_home.lemmas() == {"kept", "change"}


@pytest.mark.parametrize(
    ("program", "error"),
    [
        pytest.param("raise SystemExit(3)", subprocess.CalledProcessError, id="import_fails"),
        pytest.param("pass", RuntimeError, id="import_adds_nothing"),
    ],
)
def test_wn_lexicon_failed_import_never_reaches_the_database(
    wn_home: WnHome, monkeypatch: pytest.MonkeyPatch, program: str, error: type[Exception]
) -> None:
    monkeypatch.setattr(model_cache, "WN_IMPORT", program)

    with pytest.raises(error):
        model_cache.ensure_wn_lexicon()

    assert not wn_home.database.exists()
    assert not wn_home.stamp.exists()


def test_wn_lexicon_failed_rebuild_leaves_the_existing_database_alone(
    wn_home: WnHome, monkeypatch: pytest.MonkeyPatch
) -> None:
    wn_home.seed("other", "1.0", lemma="kept")
    before = wn_home.database.read_bytes()
    monkeypatch.setattr(model_cache, "WN_IMPORT", "raise SystemExit(3)")

    with pytest.raises(subprocess.CalledProcessError):
        model_cache.ensure_wn_lexicon()

    assert wn_home.database.read_bytes() == before
    assert not wn_home.stamp.exists()


def test_wn_lexicon_digest_mismatch_never_imports(wn_home: WnHome, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_cache.http,
        "github_download",
        lambda _url, dest: dest.write_bytes(b"wrong-oewn-archive"),
    )

    with pytest.raises(RuntimeError, match="integrity mismatch for oewn:2025\\+"):
        model_cache.ensure_wn_lexicon()

    archive = wn_home.data_dir / model_cache.WN_ARCHIVE_NAME
    assert wn_home.imports == []
    assert not wn_home.database.exists()
    assert not archive.exists()
    assert not archive.with_name(f".{archive.name}.part").exists()


def test_ensure_nlp_resources_composes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(model_cache, "ensure_spacy_model", lambda: calls.append("spacy"))
    monkeypatch.setattr(model_cache, "ensure_wn_lexicon", lambda: calls.append("wn"))

    model_cache.ensure_nlp_resources()

    assert calls == ["spacy", "wn"]
