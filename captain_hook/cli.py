from __future__ import annotations

import importlib.resources
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from cc_transcript.ids import SessionId

from captain_hook.app import _state, load_gitignore, reset
from captain_hook.context import HookContext, load_transcript
from captain_hook.dispatch import dispatch
from captain_hook.loader import CONF_MODULE, discover_hooks, discover_pack
from captain_hook.log import setup_logging
from captain_hook.packs import manager
from captain_hook.review.cli import review
from captain_hook.session import SessionStore, ensure_session
from captain_hook.types import Event

DIST_NAME = "capt-hook"
EVENT_NAMES = ", ".join(n for e in Event if (n := e.name))
MARKETPLACE = {"captain-hook": {"source": {"source": "github", "repo": "yasyf/captain-hook"}}}
PLUGIN_ID = "captain-hook@captain-hook"


@dataclass(frozen=True, slots=True)
class CliState:
    root: Path
    hooks: str

    def discover(self) -> None:
        reset()
        load_gitignore(self.root)
        discover_hooks(self.hooks)
        resolved, missing = manager.resolve_enabled_packs(self.root)
        for pack_ in resolved:
            discover_pack(pack_.entry.name, pack_.path)
        if missing:
            print(
                f"capt-hook: packs not cached: {', '.join(missing)} — run `capt-hook pack update`",
                file=sys.stderr,
            )


def example_hook_source() -> str:
    """Read the bundled ``example.py`` scaffold from ``templates/example_hook.py.tmpl``."""
    return (importlib.resources.files("captain_hook") / "templates" / "example_hook.py.tmpl").read_text()


def plugin_dir() -> Path:
    """Filesystem path to the bundled captain-hook plugin root.

    Holds ``.claude-plugin/plugin.json`` and ``skills/``, so ``claude --plugin-dir``
    can load the skills in-place from the installed wheel without a marketplace clone.
    """
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
            "extraKnownMarketplaces": existing.get("extraKnownMarketplaces", {}) | MARKETPLACE,
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


def event_names(events: Event) -> set[str]:
    return {name for member in Event if member in events and (name := member.name)}


def subscribed_events() -> set[str]:
    return {name for entry in _state.hooks for name in event_names(entry.spec.events)}


def generate_settings(hooks_dir: str = ".claude/hooks", from_source: str = DIST_NAME) -> dict[str, Any]:
    events_by_async: defaultdict[bool, set[str]] = defaultdict(set)
    for entry in _state.hooks:
        events_by_async[entry.spec.async_] |= event_names(entry.spec.events)

    from_flag = "" if from_source == DIST_NAME else f" --from {from_source}"
    hooks_flag = "" if hooks_dir == ".claude/hooks" else f" --hooks $CLAUDE_PROJECT_DIR/{hooks_dir}"

    def commands(event: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "command",
                "command": (f"uvx{from_flag} capt-hook{hooks_flag} run {event}{' --async' if is_async else ''}"),
            }
            | ({"async": True} if is_async else {})
            for is_async, events in sorted(events_by_async.items())
            if event in events
        ] + ([{"type": "command", "command": f"uvx{from_flag} capt-hook review run"}] if event == "SessionEnd" else [])

    return {
        "hooks": {
            event: [{"hooks": commands(event)}]
            for event in sorted(events_by_async[False] | events_by_async[True] | {"SessionEnd"})
        }
    }


def is_captain_hook_group(group: dict[str, Any]) -> bool:
    return any("capt-hook" in (h.get("command") or "") for h in group.get("hooks") or [])


def capt_hook_events(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        event
        for event, groups in (json.loads(path.read_text()).get("hooks") or {}).items()
        if any(is_captain_hook_group(g) for g in groups)
    }


def merge_settings(
    hooks_dir: str, settings_path: Path, from_source: str = DIST_NAME
) -> tuple[dict[str, Any], dict[str, str]]:
    new_hooks: dict[str, list[dict[str, Any]]] = generate_settings(hooks_dir, from_source=from_source)["hooks"]
    existing = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    existing_hooks: dict[str, list[dict[str, Any]]] = existing.get("hooks") or {}
    committed = capt_hook_events(settings_path.parent / "settings.json")

    summary: dict[str, str] = {}
    merged_hooks: dict[str, list[dict[str, Any]]] = {}
    for event in sorted(existing_hooks.keys() | new_hooks.keys()):
        foreign = [g for g in existing_hooks.get(event, []) if not is_captain_hook_group(g)]
        old_own = [g for g in existing_hooks.get(event, []) if is_captain_hook_group(g)]
        fresh_own = [] if event in committed else new_hooks.get(event, [])
        if event in committed and (old_own or new_hooks.get(event)):
            summary[event] = "deferred"
        elif old_own or fresh_own:
            summary[event] = (
                "unchanged"
                if old_own == fresh_own
                else "added"
                if not old_own
                else "removed"
                if not fresh_own
                else "updated"
            )
        if groups := foreign + fresh_own:
            merged_hooks[event] = groups
    return existing | {"hooks": merged_hooks}, summary


def write_settings(settings_path: Path, data: dict[str, Any]) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(f"{settings_path.suffix}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, settings_path)


def print_hook_summary(label: str, summary: dict[str, str]) -> None:
    by_status: defaultdict[str, list[str]] = defaultdict(list)
    for event, status in summary.items():
        by_status[status].append(event)
    click.echo(f"{label}:")
    if not summary:
        click.echo("  no hook entries")
    for event in by_status["added"]:
        click.echo(f"  + added {event}")
    for event in by_status["updated"]:
        click.echo(f"  ~ updated {event}")
    for event in by_status["removed"]:
        click.echo(f"  - removed {event}")
    if unchanged := by_status["unchanged"]:
        click.echo(f"  unchanged: {', '.join(unchanged)} (already present)")
    if deferred := by_status["deferred"]:
        click.echo(f"  deferred to settings.json: {', '.join(deferred)}")


def regenerate_settings(state: CliState) -> None:
    state.discover()
    settings_path = state.root / ".claude" / "settings.local.json"
    merged, summary = merge_settings(".claude/hooks", settings_path)
    write_settings(settings_path, merged)
    print_hook_summary(str(settings_path.relative_to(state.root)), summary)


def settings_drift(root: Path) -> set[str]:
    paths = [p for name in ("settings.json", "settings.local.json") if (p := root / ".claude" / name).exists()]
    return subscribed_events() - {event for p in paths for event in capt_hook_events(p)} if paths else set()


def warn_settings_drift(
    output: dict[str, Any] | None, event: Event, root: Path | None, session_dir: Path | None, *, async_: bool
) -> dict[str, Any] | None:
    if async_ or root is None or session_dir is None or event in (Event.Stop | Event.SubagentStop):
        return output
    marker = session_dir / ".drift_surfaced"
    if marker.exists():
        return output
    if not (drift := settings_drift(root)):
        return output
    marker.write_text("")
    message = (
        "captain-hook: these events have registered hooks but are not wired in .claude/settings, "
        f"so the hooks never fire: {', '.join(sorted(drift))}. "
        "Run `uvx capt-hook register-hooks` to wire them."
    )
    base = output or {"hookSpecificOutput": {"hookEventName": event.name}}
    hso = base["hookSpecificOutput"]
    hso["additionalContext"] = f"{prev}\n\n{message}" if (prev := hso.get("additionalContext")) else message
    if event is Event.PreToolUse:
        hso.setdefault("permissionDecision", "allow")
    return base


def run_event(
    event_name: str,
    *,
    async_: bool = False,
    root: Path | None = None,
) -> None:
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
    resolved_path = raw.get("agent_transcript_path") or raw.get("transcript_path")

    session_dir = ensure_session(SessionId(session_id)) if session_id else None
    ctx = HookContext(
        session=SessionStore(session_dir),
        transcript=load_transcript(resolved_path),
        settings=_state.settings,
        project_root=root,
    )
    evt = event.event_class(_raw=raw, ctx=ctx)

    if output := warn_settings_drift(
        dispatch(event, evt, session_dir=session_dir, async_=async_),
        event,
        root,
        session_dir,
        async_=async_,
    ):
        print(json.dumps(output))


def init_project(root: Path, *, review: bool = True) -> None:
    from captain_hook.review.cli import watch_repo
    from captain_hook.review.repo import repo_key

    hooks_dir = root / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    example = hooks_dir / "example.py"
    if not example.exists():
        example.write_text(example_hook_source())

    settings_path = root / ".claude" / "settings.local.json"
    CliState(root=root, hooks=str(hooks_dir)).discover()
    merged, summary = merge_settings(".claude/hooks", settings_path)
    write_settings(settings_path, merged)

    register_marketplace(root)

    click.echo(f"Scaffolded {example.relative_to(root)} + {settings_path.relative_to(root)}.")
    click.echo()
    print_hook_summary(str(settings_path.relative_to(root)), summary)
    click.echo()
    click.echo("Claude Code plugin:")
    click.echo(f"  + registered {PLUGIN_ID} in .claude/settings.json (skills install on folder-trust)")
    click.echo()
    match (review, repo_key(root)):
        case (False, _):
            click.echo("Session reviewer: skipped (--no-review) — `uvx capt-hook review enable` to turn it on later.")
        case (True, None):
            click.echo("Session reviewer: needs a git repo with a remote — `uvx capt-hook review enable` once it has one.")
        case (True, repo):
            watch_repo(repo)
            click.echo(f"Session reviewer: watching {repo} — mines your ended sessions and opens hook PRs automatically.")
            click.echo("  Stop anytime with `uvx capt-hook review disable`.")
    click.echo()
    click.echo("Next:")
    click.echo("  1. Read the quickstart: https://yasyf.github.io/captain-hook/")
    click.echo("  2. Edit example.py or add new files under .claude/hooks/")
    click.echo("  3. uvx capt-hook test               # verify inline tests")
    click.echo("  4. uvx capt-hook register-hooks     # re-register after adding events")
    click.echo('  5. Ask Claude "set up captain hook" # mine guardrails from this repo (the bootstrapping-hooks skill)')
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
    from captain_hook.settings import resolve_log_dir

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
    from captain_hook.testing.helpers import run_inline_tests

    results = run_inline_tests()
    if not results:
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
        print(f"\n{total} tests: {passed} passed, {failed} failed, {errors} errors, {skipped} skipped")
    if failed or errors:
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
    root = Path(root_path) if root_path else Path(env) if (env := os.environ.get("CLAUDE_PROJECT_DIR")) else Path.cwd()
    ctx.obj = CliState(root=root, hooks=hooks or str(root / ".claude" / "hooks"))


@cli.command(
    short_help="Dispatch a hook event (reads JSON from stdin, writes JSON to stdout)",
    help=(f"Dispatch a hook event (reads JSON from stdin, writes JSON to stdout).\n\nEVENT is one of: {EVENT_NAMES}."),
)
@click.argument("event")
@click.option("--async", "async_", is_flag=True, default=False, help="Run async hooks only")
@click.pass_obj
def run(state: CliState, event: str, async_: bool) -> None:
    state.discover()
    run_event(event, async_=async_, root=state.root)


@cli.command(name="register-hooks")
@click.option("--hooks-dir", default=".claude/hooks", help="Hooks directory relative to project root")
@click.option("--dry-run", is_flag=True, default=False, help="Print the merged settings JSON without writing")
@click.option(
    "--from",
    "from_source",
    default=DIST_NAME,
    help=f"Package source for uvx --from (local path or PyPI spec, default: {DIST_NAME})",
)
@click.pass_obj
def register_hooks_cmd(state: CliState, hooks_dir: str, dry_run: bool, from_source: str) -> None:
    """Register captain-hook's event hooks into .claude/settings.local.json."""
    state.discover()
    settings_path = state.root / ".claude" / "settings.local.json"
    merged, summary = merge_settings(hooks_dir, settings_path, from_source=from_source)
    if dry_run:
        click.echo(json.dumps(merged, indent=2))
        return
    write_settings(settings_path, merged)
    print_hook_summary(str(settings_path), summary)


@cli.command()
@click.option("--json", "json_output", is_flag=True, default=False, help="Emit one JSON record per test (CI mode)")
@click.pass_obj
def test(state: CliState, json_output: bool) -> None:
    """Run inline tests from all registered hooks."""
    state.discover()
    run_tests(json_output=json_output)


@cli.command()
@click.option("--no-review", is_flag=True, default=False, help="Skip enabling the SessionEnd session reviewer for this repo")
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

    status_command(resolve_repo(repo_, state.root), sync=sync)


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
            else manager.fetch_pack(manager.PackSource.parse(target)).entry
        )
    except manager.PackError as e:
        raise click.ClickException(str(e)) from e
    manager.upsert_entry(manager.packs_toml_path(state.root), entry)
    click.echo(f"  added {entry.name}")
    regenerate_settings(state)


@pack.command(name="list")
@click.pass_obj
def pack_list(state: CliState) -> None:
    """List the packs enabled in .claude/hooks/packs.toml."""
    resolved, missing = manager.resolve_enabled_packs(state.root)
    for r in resolved:
        match r.entry:
            case manager.BuiltinPack():
                kind, ref = "builtin", "-"
            case manager.ExternalPack(source=source, commit=commit):
                kind, ref = "github", f"{source.ref or 'HEAD'}@{commit[:7]}"
        count = sum(1 for p in r.path.glob("*.py") if not p.stem.startswith("_") and p.stem != CONF_MODULE)
        click.echo(f"  {r.entry.name:24} {kind:8} {ref:20} v{r.manifest.version:8} {count} hooks")
    for name in missing:
        click.echo(f"  {name:24} github   (not cached — run `capt-hook pack update`)")


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
    regenerate_settings(state)


@pack.command(name="update")
@click.argument("name", required=False)
@click.pass_obj
def pack_update(state: CliState, name: str | None) -> None:
    """Re-resolve external packs' refs to fresh commits and re-fetch."""
    path = manager.packs_toml_path(state.root)
    for entry in manager.read_entries(path):
        match entry:
            case manager.ExternalPack(name=n, source=source) if name in (None, n):
                manager.upsert_entry(path, fresh := manager.fetch_pack(source).entry)
                click.echo(f"  updated {n} -> {fresh.commit[:7]}")
            case manager.BuiltinPack(name=n) if name == n:
                click.echo(f"  {n} is builtin; it tracks the installed capt-hook version")
    regenerate_settings(state)


cli.add_command(review)

main = cli
