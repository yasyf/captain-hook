"""``capt-hook pause`` and ``capt-hook resume``: the sanctioned, self-expiring way to quiet every hook.

``pause`` writes ``pause.json`` into the state dir with the expiry as epoch seconds. The signed host's
``run`` client reads it before it opens a session to the daemon, so while it holds, every event of every
session — whichever plugin version it is pinned to — returns at once without dispatch, and a
``SessionStart`` prints a banner naming the expiry. Nothing has to lift it: past ``until`` the host
ignores the file. The host also ignores a pause longer than :data:`MAX_PAUSE`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import click

from captain_hook.util.fs import atomic_write, read_json
from captain_hook.util.paths import resolve_state_dir

PAUSE_FILE = "pause.json"
MAX_PAUSE = timedelta(hours=1)
DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def pause_path() -> Path:
    return resolve_state_dir() / PAUSE_FILE


def parse_duration(text: str) -> timedelta:
    if not (match := DURATION.fullmatch(text.strip())) or not any(match.groups()):
        raise click.BadParameter(f"{text!r} is not a duration like 30m, 1h, or 1h30m")
    hours, minutes, seconds = (int(group or 0) for group in match.groups())
    if not timedelta() < (span := timedelta(hours=hours, minutes=minutes, seconds=seconds)) <= MAX_PAUSE:
        raise click.BadParameter(f"a pause lasts more than 0s and at most {format_span(MAX_PAUSE)}")
    return span


def format_span(span: timedelta) -> str:
    minutes, seconds = divmod(int(span.total_seconds()), 60)
    hours, minutes = divmod(minutes, 60)
    return "".join(f"{value}{unit}" for value, unit in ((hours, "h"), (minutes, "m"), (seconds, "s")) if value) or "0s"


def active_pause(now: datetime) -> tuple[datetime, str | None] | None:
    match read_json(pause_path()):
        case {"until": int(until), "reason": str() | None as reason} if (
            timedelta() < (expiry := datetime.fromtimestamp(until).astimezone()) - now <= MAX_PAUSE
        ):
            return expiry, reason
        case _:
            return None


def describe(expiry: datetime, reason: str | None, now: datetime) -> str:
    because = f" ({reason})" if reason else ""
    return f"hooks paused until {expiry:%H:%M %Z}, {format_span(expiry - now)} from now{because}"


@click.command(name="pause")
@click.option(
    "--for", "duration", default=None, help=f"How long to pause, like 30m or 1h30m (at most {format_span(MAX_PAUSE)})"
)
@click.option("--reason", default=None, help="Why, shown in the banner every new session prints")
@click.option("--status", is_flag=True, default=False, help="Show the pause in effect instead of setting one")
def pause(duration: str | None, reason: str | None, status: bool) -> None:
    """Pause every capt-hook hook, guards included, for a while; it lifts by itself at expiry.

    The one sanctioned way to quiet hooks: never edit the plugin cache. `capt-hook resume` lifts it early.
    """
    now = datetime.now().astimezone()
    match status, duration:
        case True, None:
            click.echo(describe(*held, now) if (held := active_pause(now)) else "hooks are not paused")
        case False, str():
            expiry = now + parse_duration(duration)
            atomic_write(pause_path(), json.dumps({"until": int(expiry.timestamp()), "reason": reason}))
            click.echo(f"{describe(expiry, reason, now)}; `capt-hook resume` lifts it now")
        case _:
            raise click.UsageError("pass --for DURATION to pause, or --status alone to show the pause")


@click.command(name="resume")
def resume() -> None:
    """Lift a ``capt-hook pause`` before it expires."""
    held = active_pause(datetime.now().astimezone())
    pause_path().unlink(missing_ok=True)
    click.echo("hooks resumed" if held else "hooks were not paused")
