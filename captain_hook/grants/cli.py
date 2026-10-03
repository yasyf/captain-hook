"""``capt-hook grant``: mint, inspect, and revoke grants from the shell."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import click

from captain_hook.grants import store
from captain_hook.grants.evidence import ruling_key
from captain_hook.grants.records import Evidence, Grant
from captain_hook.util import reqenv

SPAN = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?")


def parse_span(text: str) -> timedelta:
    if not (match := SPAN.fullmatch(text.strip())) or not any(match.groups()):
        raise click.BadParameter(f"{text!r} is not a lifetime like 30m, 8h, or 7d", param_hint="--for")
    days, hours, minutes = (int(group or 0) for group in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes)


def parse_scope(pairs: tuple[str, ...]) -> dict[str, str]:
    scope: dict[str, str] = {}
    for pair in pairs:
        key, eq, value = pair.partition("=")
        if not eq or not key:
            raise click.BadParameter(f"{pair!r} is not key=value", param_hint="--scope")
        scope[key] = value
    return scope


def session_tree() -> str:
    if not (session := reqenv.getenv("CLAUDE_CODE_SESSION_ID") or reqenv.getenv("CLAUDE_SESSION_ID")):
        raise click.UsageError("pass --tree: no CLAUDE_CODE_SESSION_ID names the session this grant belongs to")
    return session


def describe(grant: Grant) -> str:
    used = store.spends(grant.id)
    at = store.now()
    state = store.unusable(grant, used, at) or (
        "live, unlimited" if grant.uses is None else f"live, {store.remaining(grant, used, at)} of {grant.uses} left"
    )
    return f"{grant.id}  {grant.kind}  {grant.scope}  tree {grant.tree}  {state}"


def mint_options[F: Callable[..., Any]](command: F) -> F:
    for option in reversed(
        [
            click.option("--kind", required=True, help="The grant kind a hook declares, e.g. slack.write"),
            click.option("--scope", "scope", multiple=True, help="One key=value of the kind's scope; repeat per key"),
            click.option("--uses", type=int, default=1, show_default=True, help="Uses the grant allows"),
            click.option("--unlimited", is_flag=True, help="No use limit: a standing grant"),
            click.option("--for", "span", default=None, help="Lifetime, like 30m, 8h, or 7d"),
            click.option("--rule", "rules", multiple=True, help="A declared rule this grant asserts; repeat"),
            click.option("--tree", default=None, help="Root session id it belongs to (default: this session)"),
        ]
    ):
        command = option(command)
    return command


@dataclass(frozen=True, slots=True)
class MintOptions:
    kind: str
    scope: tuple[str, ...]
    uses: int
    unlimited: bool
    span: str | None
    rules: tuple[str, ...]
    tree: str | None


def minted(options: MintOptions, *, evidence: Evidence, author: str) -> Grant:
    return store.mint(
        Grant(
            id=store.new_id(),
            kind=options.kind,
            tree=options.tree or session_tree(),
            scope=parse_scope(options.scope),
            uses=None if options.unlimited else options.uses,
            expires=store.expiry(parse_span(options.span) if options.span else None),
            rules=list(options.rules),
            evidence=[evidence],
            source_key=evidence.key or None,
            author=author,
            created=store.now(),
        )
    )


@click.group(name="grant")
def grant() -> None:
    """Record, inspect, and revoke spendable grants: the owner's permission as a record hooks can spend."""


def owner_at_terminal() -> None:
    if not sys.stdin.isatty():
        raise click.UsageError("only the owner mints a grant, from an interactive terminal")


@grant.command(name="add")
@mint_options
@click.option("--quote", required=True, help="The owner's words, verbatim")
def add(quote: str, **options: Any) -> None:
    """Mint a grant from the owner's verbatim words; the owner runs it from an interactive terminal.

    Agents never mint here: a hook mints a grant only from the owner's own words, through its judge.
    """
    owner_at_terminal()
    evidence = Evidence(id="cli", source="cli", quote=quote, said_at=store.now())
    click.echo(describe(minted(MintOptions(**options), evidence=evidence, author="cli")))


@grant.command(name="import")
@mint_options
@click.argument("answer_id")
def import_(answer_id: str, **options: Any) -> None:
    """Mint a grant from a cc-notes answer recording the owner's ruling, pinned to its current revision.

    The owner runs it from an interactive terminal; a hook reads rulings as evidence on its own.
    """
    owner_at_terminal()
    done = subprocess.run(
        ["ccn", "answer", "show", answer_id, "--json"], capture_output=True, text=True, check=True, env=reqenv.env_map()
    )
    answer = json.loads(done.stdout)
    evidence = Evidence(
        id=f"ccn:{answer['id'][:7]}",
        source="ccn-answer",
        quote=answer["body"],
        detail=f"ruling {answer['id'][:7]}: {answer['title']}",
        key=ruling_key(answer),
    )
    click.echo(describe(minted(MintOptions(**options), evidence=evidence, author=f"ccn:{answer['id'][:7]}")))


@grant.command(name="list")
@click.option("--kind", default=None, help="Only this kind")
@click.option("--tree", default=None, help="Only this session tree")
@click.option("--all", "everything", is_flag=True, help="Include spent, expired, and revoked grants")
def list_(kind: str | None, tree: str | None, everything: bool) -> None:
    """List grants, live ones only unless --all."""
    at = store.now()
    for found in store.grants(kind, tree):
        if everything or store.unusable(found, store.spends(found.id), at) is None:
            click.echo(describe(found))


@grant.command()
@click.argument("grant_id")
def show(grant_id: str) -> None:
    """Show a grant's record and every use of it."""
    found = store.load(grant_id)
    click.echo(describe(found))
    click.echo(found.model_dump_json(indent=2))
    for adoption in store.adoptions(grant_id):
        click.echo(f"{store.stamp(adoption.at)}  adopted into tree {adoption.tree} by {adoption.session}/{adoption.agent}")
    for spend in store.spends(grant_id):
        click.echo(
            f"{store.stamp(spend.at)}  {spend.state}  {spend.session}/{spend.agent}  {spend.summary}  {spend.reason}"
        )


@grant.command()
@click.argument("grant_id")
def revoke(grant_id: str) -> None:
    """Revoke a grant; it covers nothing from now on."""
    click.echo(describe(store.revoke(grant_id)))


@grant.command()
@click.argument("grant_id")
@click.option("--tree", default=None, help="Session tree to adopt it into (default: this session)")
@click.option("--agent", default="main", show_default=True, help="The agent adopting it, for the log")
def adopt(grant_id: str, tree: str | None, agent: str) -> None:
    """Make a grant from another session tree usable in this one; both trees share its budget.

    Any agent may adopt a grant it was handed; the adoption is logged with the session and agent.
    """
    session = session_tree()
    adoption = store.adopt(grant_id, tree=tree or session, session=session, agent=agent)
    click.echo(f"{describe(store.load(grant_id))}  adopted into {adoption.tree}")


@grant.command()
@click.argument("grant_id")
@click.option("--scope", "scope", multiple=True, required=True, help="One key=value of the action's scope; repeat")
@click.option("--tree", required=True, help="Session tree the action runs in")
@click.option("--session", required=True, help="Session making the call")
@click.option("--agent", default="main", show_default=True, help="Agent making the call")
@click.option("--call", "call", required=True, help="Id of the call this use pays for")
@click.option("--fingerprint", required=True, help="Digest of the action's payload; a retry repeats it")
@click.option("--summary", required=True, help="One line naming the action")
def spend(
    grant_id: str,
    scope: tuple[str, ...],
    tree: str,
    session: str,
    agent: str,
    call: str,
    fingerprint: str,
    summary: str,
) -> None:
    """Spend one use of a grant for a system that enforces it downstream, such as the cc-slack daemon.

    Prints the grant and the uses left as JSON; exits non-zero with the reason when the grant cannot pay.
    """
    try:
        left = store.reserve(
            grant_id,
            tree=tree,
            scope=parse_scope(scope),
            state="committed",
            session=session,
            agent=agent,
            tool_use_id=call,
            fingerprint=fingerprint,
            summary=summary,
            reason="spent downstream",
            relied_on=[],
        )
    except store.SpentError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({"grant": json.loads(store.load(grant_id).model_dump_json()), "remaining": left}))
