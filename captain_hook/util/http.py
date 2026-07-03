"""Authenticated, rate-limit-aware GitHub fetches over stdlib urllib.

Both the packs manager and the spaCy model cache fetch from GitHub. They route
through ``github_get_json`` / ``github_download`` here, which authenticate (env
``GITHUB_TOKEN`` else ``gh auth token``), retry transient failures with
exponential backoff, honor GitHub's rate-limit headers, and raise a clean
``GitHubFetchError`` instead of leaking a raw ``urllib.error.HTTPError``.
"""

from __future__ import annotations

import functools
import json
import os
import random
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

GH_TOKEN_TIMEOUT = 5
MAX_ATTEMPTS = 5
BACKOFF_BASE = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_SLEEP = 30.0
MAX_TOTAL_WAIT = 90.0
RATELIMIT_WAIT_THRESHOLD = 30.0


class GitHubFetchError(Exception):
    """A GitHub request failed after exhausting retries, or returned a non-retryable status."""


@functools.cache
def github_token() -> str | None:
    if token := os.environ.get("GITHUB_TOKEN"):
        return token
    try:
        token = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=GH_TOKEN_TIMEOUT, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return token or None


def github_headers() -> dict[str, str]:
    return {"Accept": "application/vnd.github+json", "User-Agent": "capt-hook"} | (
        {"Authorization": f"Bearer {token}"} if (token := github_token()) else {}
    )


def github_get_json(url: str) -> Any:
    def fetch() -> Any:
        with urllib.request.urlopen(urllib.request.Request(url, headers=github_headers())) as resp:
            return json.load(resp)

    return request_with_retry(fetch)


def github_download(url: str, dest: Path) -> None:
    def fetch() -> None:
        with (
            urllib.request.urlopen(urllib.request.Request(url, headers=github_headers())) as resp,
            dest.open("wb") as f,
        ):
            shutil.copyfileobj(resp, f)

    request_with_retry(fetch)


def request_with_retry[T](fetch: Callable[[], T]) -> T:
    waited = 0.0
    for attempt in range(MAX_ATTEMPTS):
        try:
            return fetch()
        except urllib.error.HTTPError as e:
            wait, error = http_wait(e, attempt), e
        except (urllib.error.URLError, OSError) as e:
            wait, error = backoff(attempt), e
        nap = min(wait, MAX_SLEEP)
        if attempt == MAX_ATTEMPTS - 1 or waited + nap > MAX_TOTAL_WAIT:
            raise GitHubFetchError(terminal_message(error)) from error
        time.sleep(nap)
        waited += nap
    raise AssertionError("request_with_retry loop is exhaustive")


def http_wait(error: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = int(value) if (value := error.headers.get("Retry-After")) and value.isdigit() else None
    match error.code:
        case 429:
            return float(retry_after) if retry_after is not None else backoff(attempt)
        case 403 if retry_after is not None:
            return float(retry_after)
        case 403 if error.headers.get("X-RateLimit-Remaining") == "0":
            return ratelimit_wait(error)
        case code if 500 <= code < 600:
            return backoff(attempt)
        case _:
            raise GitHubFetchError(f"GitHub returned {error.code} {error.reason} for {error.url}") from error


def ratelimit_wait(error: urllib.error.HTTPError) -> float:
    reset_in = int(error.headers.get("X-RateLimit-Reset", "0")) - time.time()
    if 0 < reset_in <= RATELIMIT_WAIT_THRESHOLD:
        return reset_in
    raise GitHubFetchError(
        f"GitHub API rate limit exhausted (resets in ~{max(int(reset_in), 0)}s). "
        "Set GITHUB_TOKEN or run `gh auth login` for a 5000/hr limit."
    ) from error


def backoff(attempt: int) -> float:
    return random.uniform(0, BACKOFF_BASE * BACKOFF_MULTIPLIER**attempt)


def terminal_message(error: Exception) -> str:
    match error:
        case urllib.error.HTTPError():
            return f"GitHub request failed: HTTP {error.code} {error.reason} for {error.url}"
        case urllib.error.URLError():
            return f"GitHub request failed: {error.reason}"
        case _:
            return f"GitHub request failed: {error}"
