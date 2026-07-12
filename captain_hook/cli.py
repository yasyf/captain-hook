from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from cc_transcript.ids import SessionId
from loguru import logger

from captain_hook.app import _state, load_gitignore, reset
from captain_hook.dispatch import dispatch
from captain_hook.loader import (
    CONF_MODULE,
    discover_hooks,
    discover_pack,
    is_skip_marked,
    register_nlp_provisioning,
    register_pr_announcements,
)
from captain_hook.log import setup_logging
from captain_hook.once import claim_once
from captain_hook.packs import manager
from captain_hook.review.cli import review
from captain_hook.session import SessionStore, ensure_session
from captain_hook.types import Event

if TYPE_CHECKING:
    from collections.abc import Callable

    from cc_transcript.query import Session

DIST_NAME = "capt-hook"
DEFAULT_PREFIX = f"uvx {DIST_NAME}"
EVENT_NAMES = ", ".join(n for e in Event if (n := e.name))
PLUGIN_ID = "captain-hook@captain-hook"

# Decision-capable events: their hooks can return an allow/deny/block verdict, so
# collapsing a byte-identical sibling could swallow a legitimate event and bypass its
# gate. They are never guarded — the duplicate-dispatch guard covers only pure
# side-effect events, where a missed sibling costs at most one repeated effect.
DECISION_EVENTS = frozenset({Event.PreToolUse, Event.Stop, Event.SubagentStop, Event.PermissionRequest})


@dataclass(frozen=True, slots=True)
class CliState:
    root: Path
    hooks: str

    def discover(self, session_dir: Path | None = None) -> list[manager.ResolvedPack]:
        reset()
        load_gitignore(self.root)
        discover_hooks(self.hooks)
        resolved, missing = manager.resolve_enabled_packs(self.root)
        for pack_ in resolved:
            discover_pack(pack_.entry.name, pack_.path)
        # NLP provisioning is one shared SessionStart hook; register it once, whether a
        # project (packs.toml) pack or only a session-attached pack asks for it.
        project_nlp = any(pack_.manifest.nlp for pack_ in resolved)
        if project_nlp:
            register_nlp_provisioning()
        # The PR announcer is one shared sync SessionStart hook, registered unconditionally.
        # All gating (spawned run, no repo, no DB, repo not watching) lives in
        # collect_announcements, so registration needs no settings or store inspection here.
        register_pr_announcements()
        attached = self.attached_packs(resolved, session_dir)
        for pack_ in attached:
            discover_pack(pack_.entry.name, pack_.path)
        if not project_nlp and any(pack_.manifest.nlp for pack_ in attached):
            register_nlp_provisioning()
        if missing:
            print(
                f"capt-hook: packs unavailable (offline and not cached): {', '.join(missing)} "
                "— run `capt-hook pack update` when online",
                file=sys.stderr,
            )
        return [*resolved, *attached]

    def attached_packs(
        self, resolved: Sequence[manager.ResolvedPack], session_dir: Path | None
    ) -> list[manager.ResolvedPack]:
        """Resolve session-attached packs a builtin or packs.toml pack doesn't already own.

        A packs.toml entry (builtin, source, or ``disabled = true``) of the same name wins
        over an ambient plugin attach; the shadowed attach is dropped with a debug log.
        """
        if session_dir is None:
            return []
        owned = {pack_.entry.name for pack_ in resolved} | {
            e.name
            for e in manager.read_entries(manager.packs_toml_path(self.root))
            if isinstance(e, manager.DisabledPack)
        }
        kept: list[manager.ResolvedPack] = []
        for pack_ in manager.resolve_attached(session_dir):
            if pack_.entry.name in owned:
                logger.bind(pack=pack_.entry.name).debug("attached pack shadowed by a packs.toml or disabled entry")
                continue
            kept.append(pack_)
        return kept


def example_hook_source() -> str:
    """Read the bundled ``example.py`` scaffold from ``templates/example_hook.py.tmpl``."""
    import importlib.resources

    return (importlib.resources.files("captain_hook") / "templates" / "example_hook.py.tmpl").read_text()


def plugin_dir() -> Path:
    """Filesystem path to the bundled captain-hook plugin root.

    Holds ``.claude-plugin/plugin.json`` and ``skills/``, so ``claude --plugin-dir``
    can load the skills in-place from the installed wheel without a marketplace clone.
    """
    import importlib.resources

    return Path(str(importlib.resources.files("captain_hook")))


def register_marketplace(root: Path) -> None:
    """Enable the captain-hook plugin marketplace in ``root/.claude/settings.json``.

    Merges ``extraKnownMarketplaces`` and ``enabledPlugins`` entries into the
    committed settings so the skills load from the plugin (tracking the repository)
    instead of being copied into ``.claude/skills``. Claude Code prompts to install
    the plugin when the project folder is trusted.
    """
    settings_path = root / ".claude" / "settings.json"
    existing = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    write_settings(
        settings_path,
        existing
        | {
            "extraKnownMarketplaces": existing.get("extraKnownMarketplaces", {})
            | {"captain-hook": {"source": {"source": "github", "repo": "yasyf/captain-hook"}}},
            "enabledPlugins": existing.get("enabledPlugins", {}) | {PLUGIN_ID: True},
        },
    )


def maybe_launch_bootstrap(root: Path) -> bool:
    """Offer to launch Claude with the ``bootstrapping-hooks`` skill after ``init``.

    Only fires in an interactive session with the ``claude`` CLI on PATH; CI and
    scripted runs skip the prompt entirely. On acceptance, the captain-hook plugin
    marketplace is registered in ``.claude/settings.json``, and Claude is launched
    with the bundled plugin loaded via ``--plugin-dir`` so the namespaced skill
    resolves immediately without waiting on a marketplace install.

    Returns:
        Whether Claude was launched.
    """
    if not (sys.stdin.isatty() and shutil.which("claude")):
        return False
    if not click.confirm("Bootstrap hooks now? (launches Claude with the bootstrapping-hooks skill)", default=True):
        return False
    register_marketplace(root)
    subprocess.run(
        ["claude", "--plugin-dir", str(plugin_dir()), "/captain-hook:bootstrapping-hooks"], cwd=root, check=False
    )
    return True


def run_command(event: str, *, async_: bool) -> str:
    return f"{DEFAULT_PREFIX} run {event}{' --async' if async_ else ''}"


def review_command() -> str:
    return f"{DEFAULT_PREFIX} review run"


def write_settings(settings_path: Path, data: dict[str, Any]) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(f"{settings_path.suffix}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, settings_path)


def provision_nlp(resolved: Sequence[manager.ResolvedPack]) -> None:
    import httpx
    import wn

    from captain_hook.util import http
    from captain_hook.util.model_cache import ensure_nlp_resources

    if not any(pack_.manifest.nlp for pack_ in resolved):
        return
    click.echo("Provisioning NLP resources (spaCy en_core_web_sm ~13MB + oewn:2025 lexicon ~231MB, cached)...")
    try:
        ensure_nlp_resources()
    except (http.GitHubFetchError, OSError, wn.Error, httpx.HTTPError) as e:
        click.echo(f"  deferred (offline?): {e} — the SessionStart hook will retry at session start")


def dispatch_event(
    root: Path,
    event: Event,
    raw: dict[str, Any],
    *,
    session_dir: Path | None,
    async_: bool,
    transcript_loader: Callable[[str | Path | None], Session] | None = None,
) -> dict[str, Any] | None:
    """Build the event's context and dispatch it, returning the response envelope or None.

    The one dispatch codepath shared by the cold CLI and the resident daemon: no printing,
    logging setup, discovery, or once-guard — those are the front door's job. ``transcript_loader``
    overrides the default parse (the daemon supplies a cache-backed one).
    """
    from captain_hook.context import HookContext
    from captain_hook.transcripts import lazy_transcript

    resolved_path = raw.get("agent_transcript_path") or raw.get("transcript_path")
    ctx = HookContext(
        session=SessionStore(session_dir),
        transcript=lazy_transcript(resolved_path, loader=transcript_loader),
        settings=_state.settings,
        project_root=root,
    )
    evt = event.event_class(_raw=raw, ctx=ctx)
    return dispatch(event, evt, session_dir=session_dir, async_=async_)


def run_event(state: CliState, event_name: str, *, async_: bool = False) -> None:
    try:
        event = Event[event_name]
    except KeyError:
        valid = ", ".join(n for e in Event if (n := e.name))
        print(
            f"Invalid event type: {event_name!r}. Valid event names are: {valid}",
            file=sys.stderr,
        )
        sys.exit(1)

    raw_text = sys.stdin.read()
    if not raw_text.strip():
        return

    # Collapse the N byte-identical siblings Claude Code spawns per event to one
    # dispatch. Decision-capable events (DECISION_EVENTS) are exempt: swallowing a
    # sibling there could bypass a gate, which outweighs a duplicated side effect.
    if event not in DECISION_EVENTS and not claim_once(event_name, raw_text.encode(), async_=async_):
        return

    try:
        raw = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Malformed stdin: {e}", file=sys.stderr)
        return

    session_id = raw.get("session_id")
    setup_logging(session_id)

    # stdin is parsed first so the session dir is known before discovery: CliState.discover
    # loads this session's attached packs (see attached_packs) on top of packs.toml.
    session_dir = ensure_session(SessionId(session_id)) if session_id else None
    state.discover(session_dir=session_dir)
    if output := dispatch_event(state.root, event, raw, session_dir=session_dir, async_=async_):
        print(json.dumps(output))


def init_project(root: Path, *, review: bool = True) -> None:
    from captain_hook.review.cli import watch_repo
    from captain_hook.review.repo import repo_key

    hooks_dir = root / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    example = hooks_dir / "example.py"
    if not example.exists():
        example.write_text(example_hook_source())

    settings_path = root / ".claude" / "settings.json"
    provision_nlp(CliState(root=root, hooks=str(hooks_dir)).discover())
    register_marketplace(root)

    click.echo(f"Scaffolded {example.relative_to(root)} + {settings_path.relative_to(root)}.")
    click.echo()
    click.echo("Claude Code plugin:")
    click.echo(f"  + registered {PLUGIN_ID} in .claude/settings.json (registers every hook event via the plugin)")
    click.echo()
    match (review, repo_key(root)):
        case (False, _):
            click.echo("Session reviewer: skipped (--no-review) — `uvx capt-hook review enable` to turn it on later.")
        case (True, None):
            click.echo(
                "Session reviewer: needs a git repo with a remote — `uvx capt-hook review enable` once it has one."
            )
        case (True, repo):
            watch_repo(repo)
            click.echo(
                f"Session reviewer: watching {repo} — mines your ended sessions and opens hook PRs automatically."
            )
            click.echo("  Stop anytime with `uvx capt-hook review disable`.")
    click.echo()
    click.echo("Next:")
    click.echo("  1. Read the quickstart: https://yasyf.github.io/captain-hook/")
    click.echo("  2. Edit example.py or add new files under .claude/hooks/")
    click.echo("  3. uvx capt-hook test               # verify inline tests")
    click.echo('  4. Ask Claude "set up captain hook" # mine guardrails from this repo (the bootstrapping-hooks skill)')
    click.echo()
    maybe_launch_bootstrap(root)


def show_logs(session: str | None = None, tail: int | None = None) -> None:
    """Print a captain-hook session log.

    Args:
        session: A session id, or a transcript path (whose stem is the session
            id) to locate its log file. When ``None``, the most recently
            modified log is shown.
        tail: When set, print only the last ``tail`` lines.
    """
    from captain_hook.util.paths import resolve_log_dir

    log_dir = resolve_log_dir()
    if not log_dir.exists():
        print(f"No captain-hook log directory at {log_dir}", file=sys.stderr)
        return

    if session is None:
        logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
        if not logs:
            print(f"No log files in {log_dir}", file=sys.stderr)
            return
        log_file = logs[-1]
    else:
        session_id = Path(session).stem if ("/" in session or session.endswith(".jsonl")) else session
        log_file = log_dir / f"{session_id}.log"

    if not log_file.exists():
        print(f"No log file at {log_file}", file=sys.stderr)
        return

    lines = log_file.read_text().splitlines()
    print("\n".join(lines[-tail:] if tail else lines))


def expected_kinds_from_state() -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in _state.hooks:
        if not entry.spec.tests:
            continue
        for key, expected in entry.spec.tests.items():
            out[f"{entry.name}:{key!r}"] = type(expected).__name__.lower()
    return out


def run_tests(json_output: bool = False) -> None:
    from captain_hook.app import _state
    from captain_hook.testing.helpers import run_inline_tests

    load_errors = list(_state.load_errors)
    for error in load_errors:
        line = f"{type(error.exc).__name__}: {error.exc}"
        if json_output:
            print(json.dumps({"id": error.source, "status": "load_error", "reason": line, "pack": error.pack}))
        else:
            print(f"  ERROR {f'[{error.pack}] ' if error.pack else ''}{error.source} failed to import: {line}")

    results = run_inline_tests()
    if not results:
        if load_errors:
            if not json_output:
                print(f"\n{len(load_errors)} hook file(s) failed to import.")
            sys.exit(1)
        if json_output:
            print(json.dumps({"status": "empty", "reason": "no inline tests"}))
        else:
            print("No inline tests found.")
        return

    expected_by_id = expected_kinds_from_state()
    passed = failed = errors = skipped = 0
    for name, status, _ok, detail in results:
        if json_output:
            print(
                json.dumps(
                    {
                        "id": name,
                        "status": status,
                        "expected": expected_by_id.get(name, ""),
                        "reason": detail,
                    }
                )
            )
        match status:
            case "pass":
                passed += 1
                if not json_output:
                    print(f"  PASS  {name}")
            case "skip":
                skipped += 1
                if not json_output:
                    print(f"  SKIP  {name}: {detail}")
            case "fail":
                failed += 1
                if not json_output:
                    print(f"  FAIL  {name}: {detail}")
            case "error":
                errors += 1
                if not json_output:
                    print(f"  ERROR {name}: {detail}")
            case _:
                pass

    if not json_output:
        total = passed + failed + errors + skipped
        summary = f"\n{total} tests: {passed} passed, {failed} failed, {errors} errors, {skipped} skipped"
        if load_errors:
            summary += f", {len(load_errors)} import errors"
        print(summary)
    if failed or errors or load_errors:
        sys.exit(1)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--hooks",
    default=None,
    help="Path to hooks package directory (default: $CLAUDE_PROJECT_DIR/.claude/hooks)",
)
@click.option("--root", "root_path", default=None, help="Project root for gitignore and session resolution")
@click.pass_context
def cli(ctx: click.Context, hooks: str | None, root_path: str | None) -> None:
    """Captain Hook — declarative hook framework for Claude Code lifecycle events."""
    from captain_hook.util.paths import resolve_project_dir

    root = Path(root_path) if root_path else Path(p) if (p := resolve_project_dir()) else Path.cwd()
    ctx.obj = CliState(root=root, hooks=hooks or str(root / ".claude" / "hooks"))


@cli.command(
    short_help="Dispatch a hook event (reads JSON from stdin, writes JSON to stdout)",
    help=(f"Dispatch a hook event (reads JSON from stdin, writes JSON to stdout).\n\nEVENT is one of: {EVENT_NAMES}."),
)
@click.argument("event")
@click.option("--async", "async_", is_flag=True, default=False, help="Run async hooks only")
@click.pass_obj
def run(state: CliState, event: str, async_: bool) -> None:
    run_event(state, event, async_=async_)


@cli.command()
@click.option("--json", "json_output", is_flag=True, default=False, help="Emit one JSON record per test (CI mode)")
@click.pass_obj
def test(state: CliState, json_output: bool) -> None:
    """Run inline tests from all registered hooks."""
    state.discover()
    run_tests(json_output=json_output)


@cli.command()
@click.option(
    "--no-review", is_flag=True, default=False, help="Skip enabling the SessionEnd session reviewer for this repo"
)
@click.pass_obj
def init(state: CliState, no_review: bool) -> None:
    """Scaffold the hooks directory, install bundled skills, wire settings, and enable the session reviewer."""
    init_project(state.root, review=not no_review)


@cli.command()
@click.option("--session", default=None, help="Session id or transcript path to view")
@click.option("--tail", type=int, default=None, help="Show only the last N lines")
def logs(session: str | None, tail: int | None) -> None:
    """View a recent captain-hook session log."""
    show_logs(session=session, tail=tail)


@cli.command()
@click.option("--repo", "repo_", default=None, help="Repo key (default: the current repo)")
@click.option("--sync/--no-sync", default=True, help="Refresh open PR states from GitHub in the background")
@click.pass_obj
def status(state: CliState, repo_: str | None, sync: bool) -> None:
    """Show the corrections the session reviewer is tracking and the hook PRs they would open."""
    from captain_hook.review.cli import resolve_repo
    from captain_hook.review.dashboard import status_command

    state.discover()
    status_command(resolve_repo(repo_, state.root), sync=sync, load_errors=list(_state.load_errors))


@cli.group()
def skills() -> None:
    """Manage the bundled Claude Code skills."""


@skills.command(name="install")
@click.pass_obj
def skills_install(state: CliState) -> None:
    """Register the captain-hook plugin in .claude/settings.json (skills load from the plugin, not copied files)."""
    register_marketplace(state.root)
    click.echo(f"  registered {PLUGIN_ID} in .claude/settings.json")


@cli.group()
def pack() -> None:
    """Manage capt-hook packs — named collections of hooks (builtin or from GitHub)."""


@pack.command(name="add")
@click.argument("target")
@click.pass_obj
def pack_add(state: CliState, target: str) -> None:
    """Enable a builtin pack by name, or an external pack as github:owner/repo[@ref]."""
    try:
        entry = (
            manager.BuiltinPack(name=target)
            if target in manager.builtin_packs()
            else manager.add_external(manager.PackSource.parse(target))
        )
    except manager.PackError as e:
        raise click.ClickException(str(e)) from e
    manager.upsert_entry(manager.packs_toml_path(state.root), entry)
    click.echo(f"  added {entry.name}")


@pack.command(name="attach")
@click.argument("directory")
def pack_attach(directory: str) -> None:
    """Register a plugin's pack for the current session (reads the SessionStart JSON on stdin).

    A Claude plugin wires this to a SessionStart hook so its pack loads under the byte-identical
    canonical ``uvx capt-hook run <Event>`` commands, letting Claude Code's exact-command dedup
    collapse plugin and project wiring into one process per event. Writes nothing to stdout on
    success (SessionStart stdout is injected into the model context); a missing or invalid
    manifest exits 1 with a stderr message.
    """
    raw = json.loads(sys.stdin.read())
    session_dir = ensure_session(SessionId(raw["session_id"]))
    root = Path(directory).resolve()
    try:
        manifest = manager.PackManifest.load(manager.manifest_in(root))
    except manager.PackError as e:
        raise click.ClickException(str(e)) from e
    manager.upsert_attached(
        session_dir, manager.AttachedPack(name=manifest.name, dir=str(root), version=manifest.version)
    )


@pack.command(name="list")
@click.pass_obj
def pack_list(state: CliState) -> None:
    """List the packs enabled in .claude/hooks/packs.toml."""
    resolved, missing = manager.resolve_enabled_packs(state.root)
    reset()
    for r in resolved:
        discover_pack(r.entry.name, r.path)
    for r in resolved:
        match r.entry:
            case manager.BuiltinPack():
                kind, ref = "builtin", "-"
            case manager.ExternalPack(source=source) as ext:
                kind, ref = "github", f"{source.ref or 'HEAD'}@{(manager.resolved_commit(ext) or '???')[:7]}"
        count = sum(
            1
            for p in r.path.glob("*.py")
            if not p.stem.startswith("_") and p.stem != CONF_MODULE and not is_skip_marked(p)
        )
        click.echo(f"  {r.entry.name:24} {kind:8} {ref:20} v{r.manifest.version:8} {count} hooks")
    for name in missing:
        click.echo(f"  {name:24} github   (unavailable — offline; run `capt-hook pack update` when online)")
    for error in _state.load_errors:
        click.echo(
            f"!  {error.pack}: {Path(error.source).name} failed to import - {type(error.exc).__name__}: {error.exc}"
        )


@pack.command(name="remove")
@click.argument("name")
@click.pass_obj
def pack_remove(state: CliState, name: str) -> None:
    """Disable a pack (leaves its content-addressed cache intact)."""
    try:
        manager.delete_entry(manager.packs_toml_path(state.root), name)
    except manager.PackError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"  removed {name}")


@pack.command(name="update")
@click.argument("name", required=False)
@click.pass_obj
def pack_update(state: CliState, name: str | None) -> None:
    """Re-resolve external packs' refs to fresh commits and re-fetch.

    A pack pinned with an explicit ``commit`` is re-pinned in packs.toml; a moving-ref
    pack (no ``commit``) refreshes its per-machine sidecar and stays source-only.
    """
    path = manager.packs_toml_path(state.root)
    for entry in manager.read_entries(path):
        match entry:
            case manager.ExternalPack(name=n, source=source, commit=commit) if name in (None, n):
                try:
                    sha = manager.fetched_commit(manager.fetch_pack(source))
                except manager.PackError as e:
                    raise click.ClickException(str(e)) from e
                if commit is not None:
                    manager.upsert_entry(path, manager.ExternalPack(name=n, source=source, commit=sha))
                else:
                    manager.PackMeta(commit=sha, checked_at=time.time()).write(manager.meta_path(n))
                click.echo(f"  updated {n} -> {sha[:7]}")
            case manager.BuiltinPack(name=n) if name == n:
                click.echo(f"  {n} is builtin; it tracks the installed capt-hook version")


cli.add_command(review)

main = cli
