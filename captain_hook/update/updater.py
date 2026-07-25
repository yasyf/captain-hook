"""Async self-updater: brew-upgrade the signed host when a newer release ships.

Wired as a sibling of the review dispatcher on the async SessionStart path
(:func:`dispatch_update`). To keep a multi-minute ``brew upgrade`` off the hook's thread,
the dispatch only guards, throttles via the shared :func:`_claim_stamp`, and detaches
``capt-hook update run``. The detached child (:func:`run_update`) compares the latest
``yasyf/captain-hook`` release against the installed signed host and, when the host is older,
runs ``brew upgrade --formula`` — retrying with an exact formula reinstall/install to repair a
broken deployment — then posts a success or failure banner. Every failure is a notification
or a breadcrumb; nothing here raises into the dispatch or sets an exit code.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger

from captain_hook.helper import client
from captain_hook.helper.cli import FORMULA
from captain_hook.review.pipeline import SPAWNED_ENV, _claim_stamp
from captain_hook.settings import resolve_state_dir
from captain_hook.update.settings import UpdateSettings
from captain_hook.util import reqenv
from captain_hook.util.http import GitHubFetchError, github_get_json

RELEASES_URL = "https://api.github.com/repos/yasyf/captain-hook/releases/latest"
UPDATE_STAMP = "check.stamp"
BREW_TIMEOUT = 1800.0


def update_dir() -> Path:
    return resolve_state_dir() / "update"


def update_log_path() -> Path:
    return update_dir() / "update.log"


def breadcrumb(reason: str) -> None:
    try:
        (path := update_log_path()).parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as log:
            log.write(f"{datetime.now(UTC).isoformat()} {reason}\n")
    except OSError:
        return


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.removeprefix("v").split("."))


def latest_release_tag() -> str:
    return str(github_get_json(RELEASES_URL)["tag_name"])


def installed_version() -> str | None:
    try:
        reply = client.send("ping")
    except (OSError, ValueError):
        return None
    return version if isinstance(version := reply.get("version"), str) else None


def brew(args: list[str]) -> bool:
    try:
        completed = subprocess.run(["brew", *args], capture_output=True, text=True, timeout=BREW_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        breadcrumb(f"brew {' '.join(args)} errored: {exc}")
        return False
    if completed.returncode != 0:
        breadcrumb(f"brew {' '.join(args)} exit {completed.returncode}: {completed.stderr.strip()[:200]}")
    return completed.returncode == 0


def brew_upgrade() -> bool:
    """Upgrade or repair the exact formula-owned signed deployment."""
    return (
        brew(["upgrade", "--formula", FORMULA])
        or brew(["reinstall", "--formula", FORMULA])
        or brew(["install", "--formula", FORMULA])
    )


def notify(*, kind: str, title: str, body: str) -> None:
    outcome = client.notify(kind=kind, title=title, body=body)
    if not outcome.ok:
        logger.warning(
            "capt-hook update notification not delivered", kind=kind, lane=str(outcome.lane), error=outcome.error
        )


def run_update() -> None:
    """The detached child: check the latest release and brew-upgrade an older host.

    Never raises — a network, version, or brew failure becomes a breadcrumb or an
    ``update_failed`` banner, so the async dispatch can never be affected.
    """
    try:
        latest = latest_release_tag()
    except (GitHubFetchError, KeyError, OSError) as exc:
        breadcrumb(f"update skip: release check failed: {exc}")
        return
    if (installed := installed_version()) is None:
        breadcrumb("update skip: installed host version unavailable")
        return
    try:
        current = version_tuple(installed) >= version_tuple(latest)
    except ValueError:
        breadcrumb(f"update skip: unparseable version (installed {installed}, latest {latest})")
        return
    if current:
        breadcrumb(f"update skip: host {installed} current (latest {latest})")
        return
    if brew_upgrade():
        breadcrumb(f"update ok: {installed} -> {latest}")
        notify(kind="update_installed", title="Captain Hook updated", body=f"Upgraded the signed host to {latest}.")
    else:
        breadcrumb(f"update failed: could not upgrade to {latest}")
        notify(
            kind="update_failed", title="Captain Hook update failed", body=f"Could not upgrade the host to {latest}."
        )


def update_argv() -> list[str]:
    return [sys.executable, "-m", "captain_hook", "update", "run"]


def detach() -> None:
    try:
        (log_path := update_log_path()).parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            subprocess.Popen(
                update_argv(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                cwd=reqenv.cwd(),
                env=reqenv.env_map() | {SPAWNED_ENV: "1"},
            )
    except OSError:
        breadcrumb("detach failed: update run")
        return
    breadcrumb("spawned update run")


def claim_update(settings: UpdateSettings) -> bool:
    try:
        (stamps := update_dir()).mkdir(parents=True, exist_ok=True)
        return _claim_stamp(stamps / UPDATE_STAMP, timedelta(minutes=settings.interval_minutes))
    except OSError:
        return False


def dispatch_update() -> None:
    """Async SessionStart entry (sibling of the review dispatcher): throttle and detach the updater.

    Never raises and never blocks: skips a disabled config, a spawned or headless session, claims the
    shared interval stamp so a burst of sessions triggers at most one check per window, then detaches
    ``capt-hook update run`` so a multi-minute ``brew upgrade`` runs off the hook's thread.
    """
    if not (settings := UpdateSettings()).enabled:
        return
    if reqenv.getenv(SPAWNED_ENV):
        breadcrumb("update skip: CAPT_HOOK_SPAWNED set")
        return
    if reqenv.is_headless():
        breadcrumb("update skip: sdk entrypoint")
        return
    if not claim_update(settings):
        breadcrumb("update skip: throttled")
        return
    detach()
