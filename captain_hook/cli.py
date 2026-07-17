from __future__ import annotations

import inspect
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

from captain_hook.app import AsyncDecisionError, _state, load_gitignore, reset
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
from captain_hook.packs import bootstrap, manager, plugins, scaffold
from captain_hook.packs.contract import (
    DEFAULT_PREFIX,
    DIST_NAME,
    MARKETPLACE_NAME,
    MARKETPLACE_REPO,
    PLUGIN_ID,
    VERSION_FLOOR_RE,
    search_upward,
)
from captain_hook.review.cli import review
from captain_hook.review.pipeline import DISPATCH_EVENTS, dispatch_review
from captain_hook.session import SessionStore, cleanup_stale, ensure_session
from captain_hook.types import Event

if TYPE_CHECKING:
    from collections.abc import Callable

    from cc_transcript.query import Session

    from captain_hook.types import RegisteredHook

EVENT_NAMES = ", ".join(n for e in Event if (n := e.name))

DECISION_EVENTS = frozenset({Event.PreToolUse, Event.Stop, Event.SubagentStop, Event.PermissionRequest})


@dataclass(frozen=True, slots=True)
class CliState:
    root: Path
    hooks: str

    def discover(self) -> list[manager.ResolvedPack]:
        reset()
        load_gitignore(self.root)
        discover_hooks(self.hooks)
        entries = manager.read_config_entries(self.root)
        resolved, missing = manager.resolve_enabled_packs(self.root, entries)
        plugin_packs = plugins.resolve_plugin_packs(self.root, declared={e.name for e in entries})
        packs = [*resolved, *plugin_packs]
        for pack_ in packs:
            discover_pack(pack_.entry.name, pack_.path)
        # NLP provisioning is one shared SessionStart hook; register it once when any pack asks.
        if any(pack_.manifest.nlp for pack_ in packs):
            register_nlp_provisioning()
        # The PR announcer's gating all lives in collect_announcements, so it registers unconditionally.
        register_pr_announcements()
        if missing:
            print(
                f"capt-hook: packs unavailable (offline and not cached): {', '.join(missing)} "
                "— run `capt-hook pack update` when online",
                file=sys.stderr,
            )
        # Bootstrap every pack's declared dependency marketplaces (order-preserving union);
        # maybe_bootstrap is zero-I/O on an empty union and prints nothing on any stream.
        bootstrap.maybe_bootstrap(list(dict.fromkeys(m for pack_ in packs for m in pack_.manifest.marketplaces)))
        return packs


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
    logging setup, or discovery — those are the front door's job. ``transcript_loader`` overrides
    the default parse (the daemon supplies a cache-backed one).
    """
    from captain_hook.context import HookContext
    from captain_hook.heartbeat import record_heartbeat
    from captain_hook.transcripts import lazy_transcript

    if not async_:
        record_heartbeat(event, raw)
    elif event.name in DISPATCH_EVENTS:
        try:
            dispatch_review(event.name, raw)
        except Exception:
            logger.exception("native review dispatch failed")

    resolved_path = raw.get("agent_transcript_path") or raw.get("transcript_path")
    ctx = HookContext(
        session=SessionStore(session_dir),
        transcript=lazy_transcript(resolved_path, loader=transcript_loader),
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
@click.pass_obj
def hooks(state: CliState) -> None:
    """List all discovered hooks without running their inline tests.

    Each tab-separated row contains pack, home repository, source basename, hook
    name, events, and the first line of its message or handler docstring.
    """
    from captain_hook.review.routing import CAPTAIN_HOOK_REPO, PackIndex

    state.discover()
    index = PackIndex.load(state.root)
    home_repos = {name: CAPTAIN_HOOK_REPO for name in index.builtins} | {
        route.pack_name: route.repo for route in index.externals.values()
    }
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
    from captain_hook.decisions import decisions_db_path
    from captain_hook.heartbeat import open_heartbeat_log

    if not (beats := open_heartbeat_log(decisions_db_path()).for_session(SessionId(session_id))):
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
    manager.upsert_entry(manager.config_path(state.root), entry)
    click.echo(f"  added {entry.name}")


@pack.command(name="bootstrap", hidden=True)
@click.argument("repos", nargs=-1)
def pack_bootstrap(repos: tuple[str, ...]) -> None:
    """Detached worker (spawned by discovery): register the given dependency marketplaces."""
    bootstrap.run_bootstrap(repos)


@dataclass(frozen=True, slots=True)
class LintResult:
    check: str
    ok: bool
    reason: str
    warning: bool = False  # reported, but does not fail the lint (a missing marketplace.json)


def _lint_hooks_json(root: Path, manifest_dir: Path) -> LintResult:
    # Raw-text scan, not a shape walk: the contract is zero capt-hook mention, and a walker's false
    # negatives (argv-list commands, wrapper nesting) beat a rare false positive. Absent hooks.json passes.
    present = [p for p in dict.fromkeys([root / "hooks" / "hooks.json", manifest_dir / "hooks.json"]) if p.is_file()]
    if not present:
        return LintResult("hooks.json", True, "no hooks.json — a discovered pack ships zero capt-hook invocations")
    offenders: list[str] = []
    for path in present:
        try:
            text = path.read_text()
        except OSError as e:
            return LintResult("hooks.json", False, f"{path} is unreadable: {e}")
        if DIST_NAME in text:
            offenders.append(str(path))
    if offenders:
        return LintResult(
            "hooks.json",
            False,
            f"{', '.join(offenders)} mention{'s' if len(offenders) == 1 else ''} {DIST_NAME!r} — this pack "
            f"predates the discovery contract; discovery loads a pack's hooks with no hooks.json entry, so "
            f"delete the {DIST_NAME!r} line(s)",
        )
    return LintResult("hooks.json", True, f"no {DIST_NAME!r} mention in {', '.join(str(p) for p in present)}")


def _dep_contract_reason(dep: str | dict[str, Any]) -> str | None:
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


def _lint_plugin_json(manifest_dir: Path) -> LintResult:
    if not (path := search_upward(manifest_dir, ".claude-plugin/plugin.json", "plugin.json")):
        return LintResult("plugin.json", False, f"no plugin.json found searching upward from {manifest_dir}")
    try:
        deps = json.loads(path.read_text()).get("dependencies") or []
    except (json.JSONDecodeError, OSError) as e:
        return LintResult("plugin.json", False, f"{path} is unreadable: {e}")
    # A dep references captain-hook as the bare string or as an object keyed by name or marketplace.
    named = [
        d
        for d in deps
        if d == "captain-hook"
        or (isinstance(d, dict) and (d.get("name") == "captain-hook" or d.get("marketplace") == "captain-hook"))
    ]
    if not named:
        return LintResult("plugin.json", False, f"{path} declares no captain-hook dependency")
    if reason := _dep_contract_reason(named[0]):
        return LintResult(
            "plugin.json",
            False,
            f"the captain-hook dependency in {path} {reason}; it must be an object "
            '{"name": "captain-hook", "marketplace": "captain-hook", "version": ">=X.Y.Z"}, '
            f"found {named[0]!r}",
        )
    return LintResult("plugin.json", True, f"declares the captain-hook dependency with a version floor ({path})")


def _lint_marketplace_json(manifest_dir: Path) -> LintResult:
    if not (path := search_upward(manifest_dir, ".claude-plugin/marketplace.json")):
        return LintResult("marketplace.json", True, "no marketplace.json found upward", warning=True)
    try:
        allowed = json.loads(path.read_text()).get("allowCrossMarketplaceDependenciesOn") or []
    except (json.JSONDecodeError, OSError) as e:
        return LintResult("marketplace.json", False, f"{path} is unreadable: {e}")
    if "captain-hook" in allowed:
        return LintResult("marketplace.json", True, f"allows the captain-hook cross-marketplace dependency ({path})")
    return LintResult("marketplace.json", False, f"{path} omits captain-hook from allowCrossMarketplaceDependenciesOn")


def _lint_pack_hooks(manifest: manager.PackManifest, root: Path) -> list[LintResult]:
    """Load the pack (same discovery the runtime uses) and vet its subscribed events."""
    reset()
    discover_pack(manifest.name, manifest.hooks_dir(root))
    hooks = list(_state.hooks)
    async_errors = [e for e in _state.load_errors if isinstance(e.exc, AsyncDecisionError)]
    other_errors = [e for e in _state.load_errors if not isinstance(e.exc, AsyncDecisionError)]

    # Zero hooks fails load regardless of cause — even when the async-decision check also reports
    # the rejected file, a pack that loaded nothing ships no working guard.
    if other_errors:
        load_result = LintResult(
            "load",
            False,
            f"{len(other_errors)} hook file(s) failed to load: "
            + "; ".join(f"{e.source} ({e.exc!r})" for e in other_errors),
        )
    elif not hooks:
        detail = f" ({len(async_errors)} rejected: async_=True on a decision event)" if async_errors else ""
        load_result = LintResult(
            "load", False, f"no hooks loaded from {manifest.hooks_dir(root)} — a pack ships at least one hook{detail}"
        )
    else:
        load_result = LintResult("load", True, f"{len(hooks)} hook(s) loaded, no load errors")

    async_decision = [h.name for h in hooks if h.spec.async_ and (set(h.spec.events) & DECISION_EVENTS)]
    if async_decision or async_errors:
        offenders = async_decision or [f"{e.source} ({e.exc})" for e in async_errors]
        async_result = LintResult(
            "async-decision",
            False,
            f"hook(s) {offenders} register async_=True on a decision event — the verdict is silently discarded",
        )
    else:
        async_result = LintResult("async-decision", True, "no async_=True hook on a decision event")

    return [load_result, async_result]


def lint_pack(root: Path) -> list[LintResult]:
    """Vet a plugin pack against the discovery contract; see :func:`pack_lint` for the checks."""
    # The shared resolver picks the manifest across the four layouts exactly as discovery does, so lint
    # never blames a consumer-only .claude file that discovery skips.
    location = manager.resolve_manifest(root)
    pack_root, manifest_path = location.pack_root, location.manifest
    manifest_dir = manifest_path.parent
    try:
        manifest = manager.PackManifest.load(manifest_path)
        manifest_result = LintResult("manifest", True, f"{manager.PACK_MANIFEST} resolved at {manifest_path}")
    except manager.PackError as e:
        manifest = None
        manifest_result = LintResult("manifest", False, str(e))

    results = [
        manifest_result,
        _lint_hooks_json(root, manifest_dir),
        _lint_plugin_json(manifest_dir),
        _lint_marketplace_json(manifest_dir),
    ]
    if manifest is None:
        results += [
            LintResult("load", False, "pack not loaded — manifest unresolved"),
            LintResult("async-decision", False, "pack not loaded — manifest unresolved"),
        ]
    else:
        results += _lint_pack_hooks(manifest, pack_root)
    return results


def echo_lint_results(results: list[LintResult]) -> bool:
    """Print the pass/warn/fail table for ``results``; return whether any hard failure (not a warning) was found."""
    for r in results:
        tag = "PASS " if r.ok and not r.warning else "WARN " if r.warning else "FAIL "
        click.echo(f"  {tag} {r.check}: {r.reason}")
    failures = [r for r in results if not r.ok and not r.warning]
    click.echo(f"\n{len(results)} checks: {sum(r.ok for r in results)} ok, {len(failures)} failed")
    return bool(failures)


@pack.command(name="lint")
@click.argument("plugin_root")
def pack_lint(plugin_root: str) -> None:
    """Vet a plugin's pack against the discovery contract.

    Pass the plugin root (the dir discovery loads the ``[pack]`` manifest from, at the root or one
    ``hooks/`` level below). Checks, each reported pass/fail: the capt-hook.toml manifest resolves
    (its ``marketplaces`` slugs validate here); any hooks.json under the checked layouts carries zero
    capt-hook command entries (a legacy attach or ``run`` line fails — discovery loads a pack's hooks
    with no hooks.json entry, so the migration is to delete it); plugin.json declares the captain-hook
    dependency as an object with a version floor (the mechanism that pulls the dispatcher onto a
    pack-plugin-only machine); the repo marketplace.json allows the cross-marketplace dependency (a
    warning when absent); the pack loads at least one hook with no load errors; and no hook registers
    ``async_=True`` on a decision event. Exits non-zero on any failure.
    """
    if echo_lint_results(lint_pack(Path(plugin_root).resolve())):
        sys.exit(1)


@pack.command(name="scaffold")
@click.argument("directory", default=".")
@click.option("--name", default=None, help="Pack slug (default: existing manifest name, else the directory name)")
@click.option("--description", default=None, help="Pack description (default: the existing manifest's, else generated)")
def pack_scaffold(directory: str, name: str | None, description: str | None) -> None:
    """Scaffold or migrate the three artifacts a discovered pack ships, then lint them.

    Generates any of ``capt-hook.toml`` (a ``[pack]`` manifest), ``.claude-plugin/plugin.json``,
    ``.claude-plugin/marketplace.json``, and a starter ``hooks/guard.py`` that are missing, and
    surgically repairs an existing one to satisfy the discovery contract: it adds the captain-hook
    dependency object and the marketplace allowlist entry. No ``hooks.json`` is generated — a discovered
    pack ships zero capt-hook invocations. A conforming file is left byte-for-byte unchanged; a
    present-but-unparseable file is reported and never rewritten. After writing, it runs ``pack lint``
    (exiting non-zero on any failure) and prints the two-line install snippet for your README.

    Pass the plugin root (default: the current directory) — the dir discovery loads the manifest from.
    """
    root = Path(directory).resolve()
    resolved_name = scaffold.resolve_name(root, name)
    resolved_description = scaffold.resolve_description(root, description, resolved_name)
    for action in scaffold.scaffold_pack(root, name=resolved_name, description=resolved_description):
        where = action.path.relative_to(root) if action.path.is_relative_to(root) else action.path
        click.echo(f"  {action.verb:9} {where}: {action.detail}")
    click.echo()
    if echo_lint_results(lint_pack(root)):
        sys.exit(1)
    marketplace_line, install_line = scaffold.install_snippet(root, resolved_name)
    click.echo("\nAdd to your plugin's README so users can install it:")
    click.echo(f"    {marketplace_line}")
    click.echo(f"    {install_line}")
    click.echo("\nNext steps:")
    click.echo("  1. Replace hooks/guard.py with rules that fit your project.")
    click.echo("  2. uvx capt-hook --hooks hooks test   # run your hooks' inline tests")
    click.echo("  3. uvx capt-hook pack lint .           # wire into CI")


def hook_count(path: Path) -> int:
    return sum(
        1 for p in path.glob("*.py") if not p.stem.startswith("_") and p.stem != CONF_MODULE and not is_skip_marked(p)
    )


@pack.command(name="list")
@click.pass_obj
def pack_list(state: CliState) -> None:
    """List the packs enabled in .claude/capt-hook.toml plus those discovered on enabled plugins."""
    entries = manager.read_config_entries(state.root)
    resolved, missing = manager.resolve_enabled_packs(state.root, entries)
    plugin_packs = plugins.resolve_plugin_packs(state.root, declared={e.name for e in entries})
    reset()
    for r in [*resolved, *plugin_packs]:
        discover_pack(r.entry.name, r.path)
    for r in resolved:
        match r.entry:
            case manager.BuiltinPack():
                kind, ref, version = "builtin", "-", f"v{r.manifest.version}"
            case manager.ExternalPack(source=source) as ext:
                kind = "github"
                ref = f"{source.ref or 'HEAD'}@{(manager.resolved_commit(ext) or '???')[:7]}"
                # A moving pack shows the ref it resolved to (a release tag or a branch carries its
                # own label); a pin, a pre-9.7 sidecar, or a builtin falls back to the manifest version.
                version = manager.resolved_ref_name(ext) or f"v{r.manifest.version}"
        click.echo(f"  {r.entry.name:24} {kind:8} {ref:20} {version:8} {hook_count(r.path)} hooks")
    for r in plugin_packs:
        match r.entry:
            case manager.PluginPack(name=name, plugin_id=plugin_id):
                version = f"v{r.manifest.version}"
                click.echo(f"  {name:24} {'plugin':8} {plugin_id:20} {version:8} {hook_count(r.path)} hooks")
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
        manager.delete_entry(manager.config_path(state.root), name)
    except manager.PackError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"  removed {name}")


@pack.command(name="update")
@click.argument("name", required=False)
@click.pass_obj
def pack_update(state: CliState, name: str | None) -> None:
    """Re-resolve external packs' refs to fresh commits and re-fetch.

    A pack pinned with an explicit ``commit`` is re-pinned in capt-hook.toml; a moving-ref
    pack (no ``commit``) refreshes its per-machine sidecar and stays source-only.
    """
    path = manager.config_path(state.root)
    for entry in manager.read_entries(path):
        match entry:
            case manager.ExternalPack(name=n, source=source, commit=commit) if name in (None, n):
                try:
                    fetched, resolved_ref = manager.fetch_pack(source)
                    sha = manager.fetched_commit(fetched)
                except manager.PackError as e:
                    raise click.ClickException(str(e)) from e
                if commit is not None:
                    manager.upsert_entry(path, manager.ExternalPack(name=n, source=source, commit=sha))
                else:
                    manager.PackMeta(commit=sha, checked_at=time.time(), resolved_ref=resolved_ref).write(
                        manager.meta_path(n)
                    )
                click.echo(f"  updated {n} -> {resolved_ref}@{sha[:7]}")
            case manager.BuiltinPack(name=n) if name == n:
                click.echo(f"  {n} is builtin; it tracks the installed capt-hook version")


cli.add_command(review)


@cli.group(hidden=True)
def daemon() -> None:
    """Resident per-project worker (internal — the thin client spawns and drives it)."""


@daemon.command(name="run")
@click.option("--root", "root_", required=True, help="Project root this worker serves")
@click.option("--foreground", is_flag=True, default=False, help="Log to the terminal instead of the boot log")
def daemon_run(root_: str, foreground: bool) -> None:
    """Run the resident worker, serving hook events over its per-project Unix socket."""
    from captain_hook.daemon.server import Server

    Server(Path(root_).resolve(), foreground=foreground).run()


@daemon.command(name="status")
@click.option("--root", "root_", default=None, help="Only workers serving this project root (default: this project)")
@click.option("--all", "all_", is_flag=True, default=False, help="Every worker in the run dir")
@click.option("--json", "json_output", is_flag=True, default=False, help="Emit machine-readable JSON")
def daemon_status(root_: str | None, all_: bool, json_output: bool) -> None:
    """Report resident workers: their root, pid, build, uptime, and whether each is alive (connect-only)."""
    from captain_hook.daemon import ops

    statuses = ops.status_workers(root_, all_=all_)
    if json_output:
        click.echo(json.dumps([ops.status_json(s) for s in statuses]))
        return
    if not statuses:
        click.echo("No daemon workers.")
        return
    for line in ops.format_status_table(statuses):
        click.echo(line)


@daemon.command(name="stop")
@click.option("--root", "root_", default=None, help="Only workers serving this project root (default: this project)")
@click.option("--all", "all_", is_flag=True, default=False, help="Every worker in the run dir")
def daemon_stop(root_: str | None, all_: bool) -> None:
    """Shut down resident workers over their sockets; clean the socket/meta of any dead worker (connect-only)."""
    from captain_hook.daemon import ops

    outcomes = ops.stop_workers(root_, all_=all_)
    if not outcomes:
        click.echo("No daemon workers.")
        return
    for outcome in outcomes:
        click.echo(f"  {outcome.action}: {outcome.worker.root} (pid {outcome.worker.pid})")


@daemon.command(name="restart")
@click.option("--root", "root_", default=None, help="Only workers serving this project root (default: this project)")
def daemon_restart(root_: str | None) -> None:
    """Drain resident workers so they exit after in-flight work; the next event respawns a fresh one."""
    from captain_hook.daemon import ops

    outcomes = ops.restart_workers(root_)
    if not outcomes:
        click.echo("No daemon workers.")
        return
    for outcome in outcomes:
        click.echo(f"  {outcome.action}: {outcome.worker.root} (pid {outcome.worker.pid})")


@daemon.command(name="logs")
@click.option("--root", "root_", default=None, help="Only workers serving this project root (default: this project)")
@click.option("--tail", type=int, default=None, help="Show only the last N lines of each log")
def daemon_logs(root_: str | None, tail: int | None) -> None:
    """Tail a worker's boot/stdio log and its rotated daemon log (connect-only — never spawns)."""
    from captain_hook.daemon import ops

    workers = ops.match_workers(root_, all_=False)
    if not workers:
        click.echo("No daemon workers.")
        return
    for worker in workers:
        for label, path in ops.log_sources(worker):
            click.echo(f"== {label} log: {path} ==")
            content = ops.read_tail(path, tail)
            click.echo(content if content is not None else f"(no {label} log at {path})")


main = cli
