from __future__ import annotations

import email.message
import io
import subprocess
import urllib.error
from pathlib import Path

import pytest

from captain_hook.util import http


@pytest.fixture
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(http.time, "sleep", slept.append)
    monkeypatch.setattr(http.time, "time", lambda: 1000.0)
    monkeypatch.setattr(http.random, "uniform", lambda _low, high: high)
    return slept


@pytest.fixture
def authed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "github_token", lambda: "tok")


def http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    msg = email.message.Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return urllib.error.HTTPError("https://api.github.com/x", code, "boom", msg, None)


def fake_urlopen(*outcomes: bytes | BaseException):
    results = iter(outcomes)

    def opener(_request: object, *_args: object, **_kwargs: object) -> io.BytesIO:
        if isinstance(outcome := next(results), BaseException):
            raise outcome
        return io.BytesIO(outcome)

    return opener


def test_github_token_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
    assert http.github_token() == "env-tok"


def test_github_token_falls_back_to_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    completed = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="gh-tok\n", stderr="")
    monkeypatch.setattr(http.subprocess, "run", lambda *_a, **_k: completed)
    assert http.github_token() == "gh-tok"


@pytest.mark.parametrize(
    "exc",
    [FileNotFoundError(), subprocess.CalledProcessError(1, "gh"), subprocess.TimeoutExpired("gh", 5)],
    ids=["gh-missing", "gh-unauthed", "gh-timeout"],
)
def test_github_token_degrades_to_anonymous(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def boom(*_a: object, **_k: object) -> object:
        raise exc

    monkeypatch.setattr(http.subprocess, "run", boom)
    assert http.github_token() is None


def test_github_headers_includes_auth_when_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "github_token", lambda: "tok")
    assert http.github_headers()["Authorization"] == "Bearer tok"


def test_github_headers_anonymous_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "github_token", lambda: None)
    headers = http.github_headers()
    assert "Authorization" not in headers
    assert headers["User-Agent"] == "capt-hook"


def test_get_json_success_no_retry(monkeypatch: pytest.MonkeyPatch, no_real_sleep: list[float], authed: None) -> None:
    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen(b'{"sha": "abc"}'))
    assert http.github_get_json("https://api.github.com/x") == {"sha": "abc"}
    assert no_real_sleep == []


def test_get_json_404_fails_fast(monkeypatch: pytest.MonkeyPatch, no_real_sleep: list[float], authed: None) -> None:
    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen(http_error(404)))
    with pytest.raises(http.GitHubFetchError, match="404"):
        http.github_get_json("https://api.github.com/x")
    assert no_real_sleep == []


def test_get_json_retries_5xx_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_real_sleep: list[float], authed: None
) -> None:
    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen(http_error(503), b'{"ok": true}'))
    assert http.github_get_json("https://api.github.com/x") == {"ok": True}
    assert len(no_real_sleep) == 1


@pytest.mark.parametrize(
    ("err", "slept"),
    [
        pytest.param(http_error(429, {"Retry-After": "7"}), [7.0], id="429-retry-after"),
        pytest.param(
            http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1010"}),
            [10.0],
            id="primary-ratelimit-near-reset",
        ),
    ],
)
def test_get_json_waits_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    no_real_sleep: list[float],
    authed: None,
    err: urllib.error.HTTPError,
    slept: list[float],
) -> None:
    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen(err, b'{"ok": 1}'))
    http.github_get_json("https://api.github.com/x")
    assert no_real_sleep == slept


def test_get_json_primary_ratelimit_far_reset_fails_fast(
    monkeypatch: pytest.MonkeyPatch, no_real_sleep: list[float], authed: None
) -> None:
    err = http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2000"})
    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen(err))
    with pytest.raises(http.GitHubFetchError, match="GITHUB_TOKEN"):
        http.github_get_json("https://api.github.com/x")
    assert no_real_sleep == []


def test_get_json_exhausts_attempts(monkeypatch: pytest.MonkeyPatch, no_real_sleep: list[float], authed: None) -> None:
    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen(*[http_error(503)] * http.MAX_ATTEMPTS))
    with pytest.raises(http.GitHubFetchError):
        http.github_get_json("https://api.github.com/x")
    assert len(no_real_sleep) == http.MAX_ATTEMPTS - 1


def test_download_writes_to_dest(
    monkeypatch: pytest.MonkeyPatch, no_real_sleep: list[float], authed: None, tmp_path: Path
) -> None:
    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen(b"payload"))
    dest = tmp_path / "out.bin"
    http.github_download("https://github.com/x.tar.gz", dest)
    assert dest.read_bytes() == b"payload"


def test_download_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_real_sleep: list[float], authed: None, tmp_path: Path
) -> None:
    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen(http_error(500), b"payload"))
    dest = tmp_path / "out.bin"
    http.github_download("https://github.com/x.tar.gz", dest)
    assert dest.read_bytes() == b"payload"
    assert len(no_real_sleep) == 1
