from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import click
from cc_transcript.ids import SessionId
from cc_transcript.tools import register_mcp_tool, unregister_mcp_tool
from loguru import logger

from captain_hook.app import _state, load_gitignore, reset
from captain_hook.dispatch import dispatch
from captain_hook.helper.cli import helper
from captain_hook.loader import (
    CONF_MODULE,
    discover_hooks,
    discover_pack,
    is_skip_marked,
    register_pr_announcements,
    register_resource_provisioning,
)
from captain_hook.log import setup_logging
from captain_hook.packs import manager, plugins
from captain_hook.review.cli import review
from captain_hook.review.pipeline import DISPATCH_EVENTS, dispatch_review
from captain_hook.session import SessionStore, cleanup_stale, ensure_session
from captain_hook.types import Event
from captain_hook.update.cli import update
from captain_hook.update.updater import dispatch_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from cc_transcript.query import Session

    from captain_hook.types import RegisteredHook

# capt-hook plugin/marketplace identity, rehomed from the deleted packs.contract module.
DIST_NAME = "capt-hook"
DEFAULT_PREFIX = f"uvx --isolated {DIST_NAME}"
PLUGIN_ID = "captain-hook@captain-hook"
MARKETPLACE_NAME = "captain-hook"
MARKETPLACE_REPO = "yasyf/captain-hook"
# A captain-hook dependency version floor is a lower bound: `>=X.Y.Z` (a bare pin or `<=`/`==`
# would not let a newer captain-hook resolve). `pack test` requires it in a pack plugin's plugin.json.
VERSION_FLOOR_RE = re.compile(r">=\s*\d+\.\d+\.\d+")


def search_upward(start: Path, *rel: str, stop: Path | None = None) -> Path | None:
    """The nearest existing ``base/rel`` walking from ``start`` upward; when ``stop`` is given the
    walk halts at ``stop`` (inclusive) and never ascends above it."""
    bound = stop.resolve() if stop is not None else None
    for base in (start, *start.parents):
        for r in rel:
            if (cand := base / r).is_file():
                return cand
        if bound is not None and base.resolve() == bound:
            break
    return None


EVENT_NAMES = ", ".join(n for e in Event if (n := e.name))

DECISION_EVENTS = frozenset({Event.PreToolUse, Event.Stop, Event.SubagentStop, Event.PermissionRequest})

type DiscoveryScope = Literal["all", "hooks"]


@dataclass(frozen=True, slots=True)
class CliState:
    root: Path
    hooks: str | None = None

    @property
    def hooks_dir(self) -> str:
        return self.hooks or str(self.root / ".claude" / "hooks")

    def discover(self, *, scope: DiscoveryScope = "all") -> list[manager.ResolvedPack]:
        reset()
        load_gitignore(self.root)
        discover_hooks(self.hooks_dir)
        if scope == "hooks":
            return []
        builtins = [manager.resolve_builtin(name) for name in manager.active_builtins(self.root)]
        packs = [*builtins, *plugins.resolve_plugin_packs(self.root)]
        for pack_ in packs:
            discover_pack(pack_.name, pack_.path)
        register_pack_tools(packs)
        # Resource provisioning is one shared SessionStart hook; register it once with the union.
        if resources := list(dict.fromkeys(r for pack_ in packs for r in pack_.descriptor.resources)):
            register_resource_provisioning(resources)
        # The PR announcer's gating all lives in collect_announcements, so it registers unconditionally.
        register_pr_announcements()
        return packs


ToolReg = tuple[str, dict[str, str] | None]

# cc-transcript's tool registry is a process-global; discover() and every cache hit reconcile
# against this per-name (behaves_like, span_edit) map under _registry_lock so no live spec drops.
_registered_tools: dict[str, ToolReg] = {}
_registry_lock = threading.Lock()


def pack_tool_specs(packs: Sequence[manager.ResolvedPack]) -> dict[str, ToolReg]:
    """The ``{tool_name: (behaves_like, span_edit_map)}`` map every enabled pack's ``[tools]`` declare.

    A tool name may be owned by exactly one pack; a collision across packs raises
    :class:`~captain_hook.packs.manager.PackError` naming both packs and the tool.
    """
    specs: dict[str, ToolReg] = {}
    owner: dict[str, str] = {}
    for pack_ in packs:
        for spec in pack_.descriptor.tools:
            if (prior := owner.get(spec.name)) is not None:
                raise manager.PackError(f"tool {spec.name!r} is claimed by both {prior!r} and {pack_.pack_id!r}")
            owner[spec.name] = pack_.pack_id
            specs[spec.name] = (spec.behaves_like, spec.span_edit.as_map() if spec.span_edit else None)
    return specs


def reconcile_pack_tools(desired: Mapping[str, ToolReg]) -> None:
    """Reconcile cc-transcript's tool registry to ``desired``, touching only added, removed, or changed
    tools so an unchanged spec is never re-registered and no window opens for a live tool. A strict
    no-op when ``desired`` already matches the registered set."""
    global _registered_tools
    with _registry_lock:
        for name in _registered_tools.keys() - desired.keys():
            unregister_mcp_tool(name)
        for name, (behaves_like, span_edit) in desired.items():
            if _registered_tools.get(name) != (behaves_like, span_edit):
                register_mcp_tool(name, behaves_like, span_edit)  # last write wins — no unregister gap
        _registered_tools = dict(desired)


def register_pack_tools(packs: Sequence[manager.ResolvedPack]) -> None:
    """Reconcile cc-transcript's tool registry with every enabled pack's ``[tools]`` specs."""
    reconcile_pack_tools(pack_tool_specs(packs))


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
            | {MARKETPLACE_NAME: {"source": {"source": "github", "repo": MARKETPLACE_REPO}, "autoUpdate": True}},
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


def write_settings(settings_path: Path, data: dict[str, Any]) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(f"{settings_path.suffix}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, settings_path)


def provision_pack_resources(resolved: Sequence[manager.ResolvedPack]) -> None:
    import httpx
    import wn

    from captain_hook.util import http, model_cache

    resources = list(dict.fromkeys(r for pack_ in resolved for r in pack_.descriptor.resources))
    if not resources:
        return
    click.echo(f"Provisioning pack resources ({', '.join(resources)}, cached)...")
    try:
        model_cache.provision_resources(resources)
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
    logging setup, or discovery — those are the front door's job. ``transcript_loader`` overrides
    the default parse (the daemon supplies a cache-backed one).
    """
    from captain_hook.context import HookContext
    from captain_hook.heartbeat import record_heartbeat
    from captain_hook.transcripts import lazy_transcript, registered_paths

    if not async_:
        record_heartbeat(event, raw)
    elif event in DISPATCH_EVENTS:
        try:
            dispatch_review(event.name, raw)
        except Exception:
            logger.exception("native review dispatch failed")
        if event is Event.SessionStart:
            try:
                dispatch_update()
            except Exception:
                logger.exception("native update dispatch failed")

    resolved_path = raw.get("agent_transcript_path") or raw.get("transcript_path")
    ctx = HookContext(
        session=SessionStore(session_dir),
        transcript=lazy_transcript(
            resolved_path, loader=transcript_loader, attach=lambda: registered_paths(session_dir)
        ),
        settings=_state.settings,
        project_root=root,
    )
    evt = event.event_class(_raw=raw, ctx=ctx)
    result = dispatch(event, evt, session_dir=session_dir, async_=async_)
    # Every session runs a sync SessionStart, so it is the reaping point for long-dead session dirs
    # on the one codepath shared by the cold CLI and the daemon. Fail-soft, never touch the live one.
    if not async_ and event is Event.SessionStart:
        try:
            cleanup_stale(exclude=SessionId(sid) if (sid := raw.get("session_id")) else None)
        except Exception:
            logger.opt(exception=True).debug("stale-session cleanup on SessionStart failed")
    return result


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

    try:
        raw = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Malformed stdin: {e}", file=sys.stderr)
        return

    session_id = raw.get("session_id")
    setup_logging(session_id)

    session_dir = ensure_session(SessionId(session_id)) if session_id else None
    state.discover()
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
    provision_pack_resources(CliState(root=root, hooks=str(hooks_dir)).discover())
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


def hook_message_first_line(hook: RegisteredHook) -> str:
    text = (
        hook.spec.message
        if isinstance(hook.spec.message, str)
        else (inspect.getdoc(hook.handler) or "" if hook.handler is not None else "")
    )
    return (text.splitlines() or [""])[0]


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
    ctx.obj = CliState(root=root, hooks=hooks)


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
    """Run inline tests from the project's own hooks in .claude/hooks (or --hooks).

    Only the repo's own hooks are tested here. The wheel builtins are tested by captain-hook itself,
    and a plugin's pack is tested from its own repo with `pack test`. Tests run under a throwaway HOME
    so live machine state never leaks into fixtures.
    """
    state.discover(scope="hooks")
    run_tests(json_output=json_output)


@cli.command()
@click.pass_obj
def hooks(state: CliState) -> None:
    """List all discovered hooks without running their inline tests.

    Each tab-separated row contains pack, home repository, source basename, hook
    name, events, and the first line of its message or handler docstring.
    """
    from captain_hook.review.repo import RepoKey, normalize_origin
    from captain_hook.review.routing import CAPTAIN_HOOK_REPO

    resolved = state.discover()
    # A hook's pack_name is its pack's runtime name (a builtin's name, a plugin pack's full id), so key
    # the home-repo map by that: builtins route to captain-hook, plugin packs to their plugin.json repo.
    home_repos: dict[str, RepoKey] = {}
    for r in resolved:
        match r.entry:
            case manager.BuiltinPack(name=name):
                home_repos[name] = CAPTAIN_HOOK_REPO
            case manager.PluginPack(repository=repository) if repository:
                home_repos[r.name] = RepoKey(normalize_origin(repository))
    package_root = Path(__file__).resolve().parent
    rows = [
        (
            hook.pack_name or "local",
            str(home_repos.get(hook.pack_name, "-")) if hook.pack_name else "-",
            Path(hook.source_file).name,
            hook.name,
            "|".join(event.name for event in hook.spec.events if event.name),
            hook_message_first_line(hook),
        )
        for hook in _state.hooks
        if hook.pack_name is not None or not Path(hook.source_file).resolve().is_relative_to(package_root)
    ]
    for row in sorted(rows, key=lambda row: (row[0], row[2], row[3])):
        print("\t".join(cell.replace("\t", " ") for cell in row))


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


@cli.command()
@click.option("--session", "session_id", required=True, help="Session id to inspect")
@click.pass_obj
def heartbeats(state: CliState, session_id: str) -> None:
    """Show per-event dispatch heartbeats for a session — an absent event is a wiring gap, not a quiet session."""
    import asyncio

    from cc_transcript.heartbeats import Heartbeat

    from captain_hook.decisions import decisions_db_path
    from captain_hook.heartbeat import open_heartbeat_log

    async def load() -> tuple[Heartbeat, ...]:
        async with await open_heartbeat_log(decisions_db_path()) as log:
            return await log.for_session(SessionId(session_id))

    if not (beats := asyncio.run(load())):
        click.echo(f"no dispatch heartbeats for session {session_id} (never dispatched, or a different decisions.db)")
        return
    for b in beats:
        click.echo(f"  {b.event:<22} ×{b.count}")


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
    """Inspect capt-hook packs — the wheel builtins and the packs enabled Claude plugins ship."""


def dep_contract_reason(dep: str | dict[str, Any]) -> str | None:
    """None when ``dep`` is the object-form captain-hook dependency with a version floor; else why not."""
    if not isinstance(dep, dict):
        return "must be an object, not a bare string (which carries no marketplace or version floor)"
    if not (isinstance(name := dep.get("name"), str) and name == "captain-hook"):
        return 'must set a nonempty "name" of "captain-hook"'
    if dep.get("marketplace") != "captain-hook":
        return 'must set "marketplace" to "captain-hook"'
    version = dep.get("version")
    if not (isinstance(version, str) and version.strip()):
        return 'must carry a nonempty string "version" lower bound'
    if not VERSION_FLOOR_RE.match(version.strip()):
        return f'"version" {version!r} must be a lower-bound constraint of the form ">=X.Y.Z"'
    return None


def plugin_dependency_reason(plugin_root: Path) -> str | None:
    """None when ``plugin_root``'s plugin.json declares the captain-hook dependency with a floor; else why not."""
    if not (path := plugin_root / ".claude-plugin" / "plugin.json").is_file():
        return f"no .claude-plugin/plugin.json at {plugin_root}"
    try:
        deps = json.loads(path.read_text()).get("dependencies") or []
    except (json.JSONDecodeError, OSError) as e:
        return f"{path} is unreadable: {e}"
    # A dep references captain-hook as the bare string or as an object keyed by name or marketplace.
    named = [
        d
        for d in deps
        if d == "captain-hook"
        or (isinstance(d, dict) and (d.get("name") == "captain-hook" or d.get("marketplace") == "captain-hook"))
    ]
    if not named:
        return f"{path} declares no captain-hook dependency"
    if reason := dep_contract_reason(named[0]):
        return (
            f"the captain-hook dependency in {path} {reason}; it must be an object "
            '{"name": "captain-hook", "marketplace": "captain-hook", "version": ">=X.Y.Z"}, '
            f"found {named[0]!r}"
        )
    return None


@pack.command(name="test")
@click.argument("plugin_root", default=".")
@click.option("--json", "json_output", is_flag=True, default=False, help="Emit one JSON record per test (CI mode)")
def pack_test(plugin_root: str, json_output: bool) -> None:
    """Validate and test a plugin's capt-hook pack from its working tree.

    Pass the plugin root — the dir holding ``.claude-plugin/plugin.json`` and
    ``capt-hook/{pack.toml, hooks/}`` (default: the current directory). Requires all three; validates
    the captain-hook dependency floor, the ``pack.toml`` resources and tool specs, loads only this
    working-tree pack, and runs its inline tests under a throwaway HOME. Exits non-zero on a missing
    layout, a bad dependency, an unknown resource, a load error, zero hooks, an ``async_=True`` decision
    hook, or a failing test.
    """
    from captain_hook.util.model_cache import unknown_resources

    root = Path(plugin_root).resolve()
    pack_root = root / manager.PLUGIN_PACK_DIRNAME
    descriptor_path = pack_root / manager.PACK_DESCRIPTOR
    hooks_dir = pack_root / manager.HOOKS_DIRNAME
    if not descriptor_path.is_file() or not hooks_dir.is_dir():
        raise click.ClickException(
            f"not a pack plugin root: expected {manager.PLUGIN_PACK_DIRNAME}/{manager.PACK_DESCRIPTOR} and "
            f"{manager.PLUGIN_PACK_DIRNAME}/{manager.HOOKS_DIRNAME}/ under {root}"
        )
    if reason := plugin_dependency_reason(root):
        raise click.ClickException(reason)
    try:
        descriptor = manager.PackDescriptor.load(descriptor_path)
    except manager.PackError as e:
        raise click.ClickException(f"invalid {descriptor_path}: {e}") from e
    if unknown := unknown_resources(descriptor.resources):
        raise click.ClickException(f"unknown resource(s) in {descriptor_path}: {', '.join(unknown)}")
    reset()
    discover_pack(root.name, hooks_dir)
    if not _state.hooks and not _state.load_errors:
        raise click.ClickException(f"no hooks loaded from {hooks_dir} — a pack ships at least one hook")
    run_tests(json_output=json_output)


def hook_count(path: Path) -> int:
    return sum(
        1 for p in path.glob("*.py") if not p.stem.startswith("_") and p.stem != CONF_MODULE and not is_skip_marked(p)
    )


@pack.command(name="list")
@click.pass_obj
def pack_list(state: CliState) -> None:
    """List the active wheel builtins and the pack each enabled Claude plugin ships (read-only)."""
    packs = [
        *(manager.resolve_builtin(name) for name in manager.active_builtins(state.root)),
        *plugins.resolve_plugin_packs(state.root),
    ]
    reset()
    for r in packs:
        discover_pack(r.name, r.path)
    for r in packs:
        kind = "builtin" if isinstance(r.entry, manager.BuiltinPack) else "plugin"
        click.echo(f"  {r.pack_id:34} {kind:8} {hook_count(r.path)} hooks")
    for error in _state.load_errors:
        click.echo(
            f"!  {error.pack}: {Path(error.source).name} failed to import - {type(error.exc).__name__}: {error.exc}"
        )


@cli.group()
def transcripts() -> None:
    """Register external transcripts (codex rollouts, teammate runs) into a session's deep view."""


@transcripts.command(name="register")
@click.option("--session", "session_id", required=True, help="Claude Code session id to attach the transcript to")
@click.option("--provider", default="codex", help="Transcript source provider")
@click.option("--thread-id", "thread_id", default=None, help="Provider thread id, resolved to a rollout at dispatch")
@click.option("--path", "path", default=None, help="Direct path to the transcript file")
@click.option("--label", default=None, help="Optional human label for the registration")
def transcripts_register(
    session_id: str, provider: str, thread_id: str | None, path: str | None, label: str | None
) -> None:
    """Register one external transcript against a session — exactly one of --thread-id or --path."""
    from captain_hook.transcripts import register_transcript

    if bool(thread_id) == bool(path):
        raise click.UsageError("pass exactly one of --thread-id or --path")
    try:
        entry = register_transcript(session_id, provider=provider, thread_id=thread_id, path=path, label=label)
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    click.echo(f"  registered {provider} transcript for session {session_id}: {entry.thread_id or entry.path}")


@cli.command()
def mcp() -> None:
    """Serve the capt-hook MCP server over stdio, exposing the register_transcript tool."""
    from captain_hook.mcp_server import build_mcp_server

    build_mcp_server().run()


cli.add_command(review)
cli.add_command(helper)
cli.add_command(update)


main = cli
