"""The ``capt-hook helper`` command group — install, ping, and test the desktop helper.

These are the user-facing surface for ``Captain Hook.app``: ``install`` converges the formula-owned
signed deployment, ``status`` pings the running helper, and ``notify`` fires a test banner through
:mod:`captain_hook.helper.client`. Every side effect (brew or the signed bridge) runs only
when the command is invoked, never at import.
"""

from __future__ import annotations

import subprocess

import click

from captain_hook.helper import FORMULA, client
from captain_hook.update.updater import brew, deploy, installed_version


@click.group()
def helper() -> None:
    """Manage the Captain Hook desktop notification helper."""


def cellar_version() -> str:
    """The version Homebrew holds in the Cellar for :data:`captain_hook.helper.FORMULA`."""
    listed = subprocess.run(["brew", "list", "--versions", FORMULA], capture_output=True, text=True, check=True)
    return listed.stdout.split()[-1]


@helper.command()
def install() -> None:
    """Install or repair the exact signed helper deployment via Homebrew."""
    if not brew(["install", "--formula", FORMULA]):
        brew(["reinstall", "--formula", FORMULA])
    cellar = cellar_version()
    if (host := deploy(cellar)) is None:
        raise click.ClickException(
            f"Homebrew holds {cellar} but the running helper reports "
            f"{installed_version() or 'no version (unreachable)'} — the deployment did not converge. "
            "The Cellar copy is not the deployment; nothing is installed until the host answers."
        )
    click.echo(
        f"Captain Hook {host} installed. Grant notification permission under "
        "System Settings > Notifications > Captain Hook, then add the review widget via "
        "Edit Widgets in Notification Center."
    )


@helper.command()
def status() -> None:
    """Ping the running helper and print its reported version."""
    try:
        reply = client.send("ping")
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
    """Send a test notification through the deployed helper."""
    outcome = client.notify(kind=kind, title=title, subtitle=subtitle, body=body, url=url, repo=repo)
    click.echo(f"lane={outcome.lane} ok={outcome.ok} error={outcome.error}")
