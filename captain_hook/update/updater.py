"""Async self-updater: brew-upgrade the signed host when a newer release ships.

Wired as a sibling of the review dispatcher on the async SessionStart path
(:func:`dispatch_update`). To keep a multi-minute ``brew upgrade`` off the hook's thread,
the dispatch only guards, throttles via the shared :func:`_claim_stamp`, and detaches
``capt-hook update run``. The detached child (:func:`run_update`) compares the latest
``yasyf/captain-hook`` release against the installed signed host and, when the host is older,
runs ``brew upgrade --formula`` and then :func:`deploy` — escalating to an exact formula
reinstall/install to repair a broken Cellar — then posts a success or failure banner. Every
failure is a notification or a breadcrumb; nothing here raises into the dispatch or sets an
exit code.

Every outcome is read back from the host, never from brew's exit status. ``brew upgrade``
exits 0 against a Cellar that is already current, so exit 0 covers both a real upgrade and a
no-op that left the deployment untouched — the state this converges, where one machine carried
Cellar 12.21.6 for six days while its host stayed 12.21.4 and every check logged success. Brew
only fills the Cellar; :func:`deploy` is what supersedes the deployed app, and the escalation
repairs a Cellar that brew left short. It is bounded per release tag by :data:`MAX_ESCALATIONS`:
a reinstall is heavy, and one that cannot converge must not run on every window forever.

Checking and acting are separate lanes because superseding the daemon interrupts every session it
serves. Every session checks; only one that may take hook dispatch down acts. A headless session
runs :func:`run_update` with ``apply=False``, which records the divergence and stops; the next
interactive session picks that deferral up and converges.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger

from captain_hook.helper import FORMULA, client
from captain_hook.review.pipeline import SPAWNED_ENV, _claim_stamp
from captain_hook.settings import resolve_state_dir
from captain_hook.update.settings import UpdateSettings
from captain_hook.util import reqenv
from captain_hook.util.http import GitHubFetchError, github_get_json

RELEASES_URL = "https://api.github.com/repos/yasyf/captain-hook/releases/latest"
UPDATE_STAMP = "check.stamp"
APPLY_STAMP = "apply.stamp"
ESCALATION_RECORD = "escalation"
PENDING_RECORD = "pending"
MAX_ESCALATIONS = 3
BREW_TIMEOUT = 1800.0
CELLAR_HOST = "libexec/Captain Hook.app/Contents/Helpers/capt-hookd"


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


def host_at_least(target: str) -> str | None:
    """The running host's version once it has reached ``target``; ``None`` while it has not.

    :func:`deploy` supersedes the deployed app and returns only once the new host answers a ping,
    so the host's own reply is the proof that a brew lane landed rather than no-opped.
    """
    if (installed := installed_version()) is None:
        return None
    try:
        return installed if version_tuple(installed) >= version_tuple(target) else None
    except ValueError:
        return None


def deploy(target: str) -> str | None:
    """Land the Cellar's application over the deployment, and the host's version once it reaches ``target``.

    ``package-install`` was the formula's ``post_install`` step until Homebrew's post-install sandbox
    denied the ``~/Library/LaunchAgents`` write it makes, failing every ``brew`` lane on every host.
    Outside that sandbox the same command installs, activates, and pings, so the deployment converges
    here rather than inside brew — which is why ``brew`` alone never proves anything.
    """
    try:
        prefix = subprocess.run(
            ["brew", "--prefix", FORMULA], capture_output=True, text=True, timeout=BREW_TIMEOUT, check=False
        )
        if prefix.returncode != 0:
            breadcrumb(f"brew --prefix exit {prefix.returncode}: {prefix.stderr.strip()[:200]}")
            return None
        landed = subprocess.run(
            [str(Path(prefix.stdout.strip()) / CELLAR_HOST), "package-install"],
            capture_output=True,
            text=True,
            timeout=BREW_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        breadcrumb(f"package-install errored: {exc}")
        return None
    if landed.returncode != 0:
        breadcrumb(f"package-install exit {landed.returncode}: {landed.stderr.strip()[:200]}")
    return host_at_least(target)


def pending() -> str | None:
    """The release a check-only run deferred to the next session allowed to act on it."""
    try:
        return (update_dir() / PENDING_RECORD).read_text().strip() or None
    except OSError:
        return None


def record_pending(target: str) -> None:
    try:
        (record := update_dir() / PENDING_RECORD).parent.mkdir(parents=True, exist_ok=True)
        record.write_text(target)
    except OSError:
        breadcrumb(f"pending record unwritable for {target}")


def clear_pending() -> None:
    try:
        (update_dir() / PENDING_RECORD).unlink(missing_ok=True)
    except OSError:
        breadcrumb("pending record unclearable")


def escalations(target: str) -> int:
    """Reinstall escalations already spent on ``target``; a newer tag starts a fresh budget."""
    try:
        tag, spent = (update_dir() / ESCALATION_RECORD).read_text().split()
        return int(spent) if tag == target else 0
    except (OSError, ValueError):
        return 0


def record_escalation(target: str, spent: int) -> None:
    try:
        (record := update_dir() / ESCALATION_RECORD).parent.mkdir(parents=True, exist_ok=True)
        record.write_text(f"{target} {spent}")
    except OSError:
        breadcrumb(f"escalation record unwritable for {target}")


def settled(previous: str, host: str) -> None:
    breadcrumb(f"update ok: {previous} -> {host}")
    notify(kind="update_installed", title="Captain Hook updated", body=f"Upgraded the signed host to {host}.")


def notify(*, kind: str, title: str, body: str) -> None:
    outcome = client.notify(kind=kind, title=title, body=body)
    if not outcome.ok:
        logger.warning(
            "capt-hook update notification not delivered", kind=kind, lane=str(outcome.lane), error=outcome.error
        )


def run_update(*, apply: bool = True) -> None:
    """The detached child: check the latest release and, when allowed to act, converge onto it.

    ``apply=False`` is the agent-session lane. It runs the same release check and records the
    divergence it finds, but never runs a brew lane: converging supersedes the running daemon, and
    an agent-launched session is precisely where that would drain hook dispatch under work already
    in flight. The next session that may act picks the deferral up from :func:`pending`.

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
        clear_pending()
        breadcrumb(f"update skip: host {installed} current (latest {latest})")
        return
    if not apply:
        record_pending(latest)
        breadcrumb(f"update deferred: host {installed} short of {latest}; an agent session may not supersede")
        return
    clear_pending()
    brew(["upgrade", "--formula", FORMULA])
    if (host := deploy(latest)) is not None:
        settled(installed, host)
        return
    if (spent := escalations(latest)) >= MAX_ESCALATIONS:
        breadcrumb(f"update stalled: host {installed} short of {latest} after {spent} escalations")
        return
    record_escalation(latest, spent + 1)
    for lane in ("reinstall", "install"):
        brew([lane, "--formula", FORMULA])
        if (host := deploy(latest)) is not None:
            settled(installed, host)
            return
    breadcrumb(f"update failed: host {installed} did not reach {latest}")
    notify(kind="update_failed", title="Captain Hook update failed", body=f"Could not upgrade the host to {latest}.")


def update_argv(*, apply: bool) -> list[str]:
    # -P: detach() runs this from the session's repo, and `-m` would otherwise put that repo at the
    # head of sys.path, where a directory sharing a dependency's name shadows the installed one.
    return [sys.executable, "-P", "-m", "captain_hook", "update", "run", *([] if apply else ["--check-only"])]


def detach(*, apply: bool) -> None:
    try:
        (log_path := update_log_path()).parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            subprocess.Popen(
                update_argv(apply=apply),
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
    breadcrumb(f"spawned update run{'' if apply else ' (check only)'}")


def claim(stamp: str, settings: UpdateSettings) -> bool:
    try:
        (stamps := update_dir()).mkdir(parents=True, exist_ok=True)
        return _claim_stamp(stamps / stamp, timedelta(minutes=settings.interval_minutes))
    except OSError:
        return False


def dispatch_update() -> None:
    """Async SessionStart entry (sibling of the review dispatcher): throttle and detach the updater.

    Never raises and never blocks: skips a disabled config and a spawned session, claims an interval
    stamp so a burst of sessions triggers at most one run per window, then detaches
    ``capt-hook update run`` so a multi-minute ``brew upgrade`` runs off the hook's thread.

    Every session checks; only a session that may supersede the daemon acts. A headless one detaches
    the check-only lane, which records what it finds without running a brew lane, so an
    agent-launched session can never interrupt hook dispatch in flight. An interactive session
    carrying a deferral acts on it immediately, claiming :data:`APPLY_STAMP` instead of waiting out a
    check window a headless peer already claimed — one apply per window, whichever session brings it.
    """
    if not (settings := UpdateSettings()).enabled:
        return
    if reqenv.getenv(SPAWNED_ENV):
        breadcrumb("update skip: CAPT_HOOK_SPAWNED set")
        return
    apply = not reqenv.is_headless()
    if apply and pending() is not None:
        if not claim(APPLY_STAMP, settings):
            breadcrumb("update skip: a deferred update is already claimed")
            return
    elif not claim(UPDATE_STAMP, settings):
        breadcrumb("update skip: throttled")
        return
    detach(apply=apply)
