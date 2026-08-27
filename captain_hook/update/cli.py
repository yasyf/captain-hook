"""The ``capt-hook update`` command group — the async self-updater's child entry."""

from __future__ import annotations

import click


@click.group()
def update() -> None:
    """Self-update the signed Captain Hook host from the latest GitHub release."""


@update.command(name="run")
@click.option("--check-only", is_flag=True, help="Record a newer release for the next session to act on.")
def run_hook(check_only: bool) -> None:
    """Check the latest release and brew-upgrade an older host (spawned by the SessionStart dispatch)."""
    from captain_hook.update.updater import run_update

    run_update(apply=not check_only)
