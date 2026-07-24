"""The ``capt-hook update`` command group — the async self-updater's child entry."""

from __future__ import annotations

import click


@click.group()
def update() -> None:
    """Self-update the signed Captain Hook host from the latest GitHub release."""


@update.command(name="run")
def run_hook() -> None:
    """Check the latest release and brew-upgrade an older host (spawned by the SessionStart dispatch)."""
    from captain_hook.update.updater import run_update

    run_update()
