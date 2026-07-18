"""The ``capt-hook helper`` command group — install, ping, and test the desktop helper.

These are the user-facing surface for ``Captain Hook.app``: ``install`` taps the cask and
launches it, ``status`` pings the running helper, and ``notify`` fires a test banner through
:mod:`captain_hook.helper.client`. Every side effect (brew, ``open``, the socket) runs only
when the command is invoked, never at import.
"""

from __future__ import annotations

import click

from captain_hook.helper import client

BREW_TAP = "yasyf/tap"
CASK = "capt-hook-helper"
APP_PATH = "/Applications/Captain Hook.app"


@click.group()
def helper() -> None:
    """Manage the Captain Hook desktop notification helper."""


@helper.command()
def install() -> None:
    """Install or upgrade the signed helper via Homebrew, then launch it in the background."""
    import subprocess

    subprocess.run(["brew", "tap", BREW_TAP], check=True)
    subprocess.run(["brew", "install", "--cask", "--force", CASK], check=True)
    if subprocess.run(["open", "-g", APP_PATH], capture_output=True).returncode != 0:
        subprocess.run(["open", "-g", "-a", client.APP_NAME], check=False)
    click.echo(
        "Captain Hook installed. Grant notification permission under "
        "System Settings > Notifications > Captain Hook, then add the review widget via "
        "Edit Widgets in Notification Center."
    )


@helper.command()
def status() -> None:
    """Ping the running helper and print its reported version."""
    try:
        reply = client.send({"v": client.PROTOCOL, "op": "ping"})
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"helper not reachable: {exc}") from exc
    click.echo(f"helper v{reply.get('version')} ok={reply.get('ok')}")


@helper.command()
@click.option("--kind", default="pr_open", help="Notification kind (pr_open/pr_merged/review_failure)")
@click.option("--title", default="Captain Hook test", help="Banner title")
@click.option("--subtitle", default=None, help="Banner subtitle")
@click.option("--body", default="This is a test notification", help="Banner body")
@click.option("--url", default=None, help="Click-through URL")
@click.option("--repo", default=None, help="Repo key threading related notifications")
def notify(kind: str, title: str, subtitle: str | None, body: str | None, url: str | None, repo: str | None) -> None:
    """Send a test notification through the helper, launching it if needed."""
    outcome = client.notify(kind=kind, title=title, subtitle=subtitle, body=body, url=url, repo=repo)
    click.echo(f"lane={outcome.lane} ok={outcome.ok} error={outcome.error}")
