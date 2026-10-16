"""``capt-hook ticket``: inspect, revoke, spend, and release tickets from the shell."""

from __future__ import annotations

import json

import click

from captain_hook.tickets import store
from captain_hook.tickets.records import Ticket


def parse_place(pairs: tuple[str, ...]) -> dict[str, str]:
    place: dict[str, str] = {}
    for pair in pairs:
        key, eq, value = pair.partition("=")
        if not eq or not key:
            raise click.BadParameter(f"{pair!r} is not key=value", param_hint="--place")
        place[key] = value
    return place


def describe(ticket: Ticket) -> str:
    state = str(store.unusable(ticket, store.now()) or "live")
    return f"{ticket.id}  {ticket.kind}  {ticket.place}  {ticket.reach}  {ticket.rule}  tree {ticket.tree}  {state}"


@click.group(name="ticket")
def ticket() -> None:
    """Inspect, revoke, spend, and release tickets: the record that an action, or a place, is permitted."""


@ticket.command(name="list")
@click.option("--kind", default=None, help="Only this kind")
@click.option("--tree", default=None, help="Only this session tree")
@click.option("--all", "everything", is_flag=True, help="Include expired and revoked tickets")
def list_(kind: str | None, tree: str | None, everything: bool) -> None:
    """List tickets, live ones only unless --all."""
    at = store.now()
    for found in store.tickets(kind, () if tree is None else (tree,)):
        if everything or store.unusable(found, at) is None:
            click.echo(describe(found))


@ticket.command()
@click.argument("ticket_id")
def show(ticket_id: str) -> None:
    """Show a ticket's record and every use of it."""
    found = store.load(ticket_id)
    click.echo(describe(found))
    click.echo(found.model_dump_json(indent=2))
    for use in store.uses(ticket_id):
        click.echo(f"{store.stamp(use.at)}  {use.state}  {use.session}/{use.agent}  {use.call}  {use.summary}")


@ticket.command()
@click.argument("ticket_id")
def revoke(ticket_id: str) -> None:
    """Revoke a ticket; it covers nothing from now on."""
    click.echo(describe(store.revoke(ticket_id)))


@ticket.command()
@click.argument("ticket_id")
@click.option("--place", "place", multiple=True, required=True, help="One key=value of the action's place; repeat")
@click.option("--tree", required=True, help="Session tree the action runs in")
@click.option("--session", required=True, help="Session making the call")
@click.option("--agent", default="main", show_default=True, help="Agent making the call")
@click.option("--call", "call", required=True, help="Id of the call this use records")
@click.option("--fingerprint", required=True, help="Digest of the action; a once ticket covers only its own")
@click.option("--summary", required=True, help="One line naming the action")
def spend(
    ticket_id: str,
    place: tuple[str, ...],
    tree: str,
    session: str,
    agent: str,
    call: str,
    fingerprint: str,
    summary: str,
) -> None:
    """Record a use of a ticket for a system that enforces it downstream, such as the cc-slack daemon.

    Prints the ticket as JSON; exits non-zero with the reason when the ticket does not cover the action.
    """
    try:
        found = store.spend(
            ticket_id,
            trees=(tree,),
            place=parse_place(place),
            fingerprint=fingerprint,
            session=session,
            agent=agent,
            call=call,
            summary=summary,
        )
    except store.SpentError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({"ticket": json.loads(found.model_dump_json())}))


@ticket.command()
@click.argument("ticket_id")
@click.option("--call", "call", required=True, help="Id of the call whose use never took effect")
def release(ticket_id: str, call: str) -> None:
    """Hand back the use a downstream system recorded for a call that never took effect.

    Prints the ticket and the uses released as JSON. Run it only when the action certainly did not
    happen, such as a write the remote system refused.
    """
    released = store.release(ticket_id, call)
    found = store.load(ticket_id)
    click.echo(json.dumps({"ticket": json.loads(found.model_dump_json()), "released": released}))
