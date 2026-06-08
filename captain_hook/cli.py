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

from captain_hook.app import _state, load_gitignore, reset
from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch
from captain_hook.loader import discover_hooks
from captain_hook.log import setup_logging
from captain_hook.session import SessionStore, ensure_session
from captain_hook.transcript import Transcript
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


def example_hook_source() -> str:
    """Read the bundled ``example.py`` scaffold from ``templates/example_hook.py.tmpl``."""
    return (importlib.resources.files("captain_hook") / "templates" / "example_hook.py.tmpl").read_text()


def install_skills(root: Path, *, force: bool = False) -> dict[str, str]:
    """Copy the bundled Claude Code skills into ``root/.claude/skills``.

    Args:
        root: Project root receiving the skills.
        force: Replace existing skill directories wholesale instead of skipping them.

    Returns:
        Per-skill status of ``"installed"``, ``"replaced"``, or ``"skipped"``.
    """
    dest_root = root / ".claude" / "skills"
    summary: dict[str, str] = {}
    with importlib.resources.as_file(importlib.resources.files("captain_hook") / "skills") as src_root:
        for skill in sorted(p for p in src_root.iterdir() if p.is_dir()):
            dest = dest_root / skill.name
            if dest.exists() and not force:
                summary[skill.name] = "skipped"
                continue
            if dest.exists():
                shutil.rmtree(dest)
                summary[skill.name] = "replaced"
            else:
                summary[skill.name] = "installed"
            shutil.copytree(skill, dest)
    return summary


def register_marketplace(root: Path) -> None:
    """Enable the captain-hook plugin marketplace in ``root/.claude/settings.local.json``.

    Merges ``extraKnownMarketplaces`` and ``enabledPlugins`` entries into the
    existing settings so the bundled skills track the repository as a plugin.
    """
    settings_path = root / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    merged = existing | {
        "extraKnownMarketplaces": existing.get("extraKnownMarketplaces", {}) | MARKETPLACE,
        "enabledPlugins": existing.get("enabledPlugins", {}) | {PLUGIN_ID: True},
    }
    settings_path.write_text(json.dumps(merged, indent=2) + "\n")


def maybe_launch_bootstrap(root: Path) -> bool:
    """Offer to launch Claude with the ``bootstrapping-hooks`` skill after ``init``.

    Only fires in an interactive session with the ``claude`` CLI on PATH; CI and
    scripted runs skip the prompt entirely. On acceptance, the captain-hook plugin
    marketplace is registered in ``.claude/settings.local.json`` before launching.

    Returns:
        Whether Claude was launched.
    """
    if not (sys.stdin.isatty() and shutil.which("claude")):
        return False
    if not click.confirm("Bootstrap hooks now? (launches Claude with the bootstrapping-hooks skill)", default=True):
        return False
    register_marketplace(root)
    subprocess.run(["claude", "/bootstrapping-hooks"], cwd=root, check=False)
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
        ]

    return {
        "hooks": {
            event: [{"hooks": commands(event)}] for event in sorted(events_by_async[False] | events_by_async[True])
        }
    }


def generate_settings_json(hooks_dir: str = ".claude/hooks", from_source: str = DIST_NAME) -> str:
    return json.dumps(generate_settings(hooks_dir, from_source=from_source), indent=2)


def merge_settings(hooks_dir: str, settings_path: Path, from_source: str = DIST_NAME) -> dict[str, Any]:
    hook_settings = generate_settings(hooks_dir, from_source=from_source)
    if settings_path.exists():
        existing = json.loads(settings_path.read_text())
        existing["hooks"] = hook_settings["hooks"]
        return existing
    return hook_settings


def is_captain_hook_group(group: dict[str, Any]) -> bool:
    return any("capt-hook" in (h.get("command") or "") for h in group.get("hooks") or [])


def merge_init_settings(
    hooks_dir: str, settings_path: Path, from_source: str = DIST_NAME
) -> tuple[dict[str, Any], dict[str, str]]:
    hook_settings = generate_settings(hooks_dir, from_source=from_source)
    new_hooks: dict[str, list[dict[str, Any]]] = hook_settings["hooks"]

    if not settings_path.exists():
        return hook_settings, {event: "added" for event in new_hooks}

    existing = json.loads(settings_path.read_text())
    existing_hooks = existing.setdefault("hooks", {})
    summary: dict[str, str] = {}

    for event, new_entries in new_hooks.items():
        existing_entries = existing_hooks.get(event, [])
        if any(is_captain_hook_group(g) for g in existing_entries):
            summary[event] = "unchanged"
        else:
            existing_hooks[event] = existing_entries + new_entries
            summary[event] = "added"

    return existing, summary


def settings_drift(root: Path) -> set[str]:
    settings = [p for name in ("settings.json", "settings.local.json") if (p := root / ".claude" / name).exists()]
    if not settings:
        return set()
    wired = {
        event
        for path in settings
        for event, groups in (json.loads(path.read_text()).get("hooks") or {}).items()
        if any(is_captain_hook_group(g) for g in groups)
    }
    return subscribed_events() - wired


def warn_settings_drift(
    output: dict[str, Any] | None, event: Event, root: Path | None, session_dir: Path, *, async_: bool
) -> dict[str, Any] | None:
    if async_ or root is None or event in (Event.Stop | Event.SubagentStop):
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
        "Run `uvx capt-hook generate-settings` to wire them."
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

    transcript_path = raw.get("transcript_path")
    setup_logging(transcript_path)
    resolved_path = raw.get("agent_transcript_path") or transcript_path

    session_dir = ensure_session(transcript_path) if transcript_path else ensure_session(root or Path.cwd())
    ctx = HookContext(
        session=SessionStore(session_dir),
        transcript=Transcript.from_path(resolved_path),
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


def init_project(root: Path) -> None:
    hooks_dir = root / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    example = hooks_dir / "example.py"
    example_created = not example.exists()
    if example_created:
        example.write_text(example_hook_source())

    settings_path = root / ".claude" / "settings.local.json"
    reset()
    discover_hooks(str(hooks_dir))
    merged, summary = merge_init_settings(".claude/hooks", settings_path)
    settings_path.write_text(json.dumps(merged, indent=2) + "\n")

    skills_summary = install_skills(root)

    print(f"Scaffolded {example.relative_to(root)} + {settings_path.relative_to(root)}.")
    print()
    print(f"{settings_path.relative_to(root)}:")
    added = [e for e, status in summary.items() if status == "added"]
    unchanged = [e for e, status in summary.items() if status == "unchanged"]
    if not added and not unchanged:
        print("  no hook entries to add")
    for event in added:
        print(f"  + added {event} hook entry")
    if unchanged:
        print(f"  unchanged: {', '.join(unchanged)} (already present)")
    print()
    print(".claude/skills/:")
    for name in (n for n, status in skills_summary.items() if status == "installed"):
        print(f"  + installed {name}")
    if skipped := [n for n, status in skills_summary.items() if status == "skipped"]:
        print(f"  unchanged: {', '.join(skipped)} (already present; capt-hook skills install --force to refresh)")
    print()
    print("Next:")
    print("  1. Read the quickstart: https://yasyf.github.io/captain-hook/")
    print("  2. Edit example.py or add new files under .claude/hooks/")
    print("  3. uvx capt-hook test       # verify inline tests")
    print("  4. uvx capt-hook generate-settings    # rewire after adding events")
    print("  5. /bootstrapping-hooks in Claude  # mine hooks from this repo's conventions")
    print()
    maybe_launch_bootstrap(root)


def show_logs(session: str | None = None, tail: int | None = None) -> None:
    """Print a captain-hook session log.

    Args:
        session: A session id, or a transcript path (hashed via ``session_hash``)
            to locate its log file. When ``None``, the most recently modified log
            is shown.
        tail: When set, print only the last ``tail`` lines.
    """
    from captain_hook.session import session_hash
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
        session_id = session_hash(session) if ("/" in session or session.endswith(".jsonl")) else session
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


@cli.command(name="generate-settings")
@click.option("--hooks-dir", default=".claude/hooks", help="Hooks directory relative to project root")
@click.option("--no-merge", is_flag=True, default=False, help="Output standalone JSON instead of merging")
@click.option(
    "--from",
    "from_source",
    default=DIST_NAME,
    help=f"Package source for uvx --from (local path or PyPI spec, default: {DIST_NAME})",
)
@click.pass_obj
def generate_settings_cmd(state: CliState, hooks_dir: str, no_merge: bool, from_source: str) -> None:
    """Generate Claude Code settings JSON for .claude/settings.local.json."""
    state.discover()
    if no_merge:
        click.echo(generate_settings_json(hooks_dir, from_source=from_source))
    else:
        settings_path = state.root / ".claude" / "settings.local.json"
        click.echo(json.dumps(merge_settings(hooks_dir, settings_path, from_source=from_source), indent=2))


@cli.command()
@click.option("--json", "json_output", is_flag=True, default=False, help="Emit one JSON record per test (CI mode)")
@click.pass_obj
def test(state: CliState, json_output: bool) -> None:
    """Run inline tests from all registered hooks."""
    state.discover()
    run_tests(json_output=json_output)


@cli.command()
@click.pass_obj
def init(state: CliState) -> None:
    """Scaffold the hooks directory, install bundled skills, and wire settings."""
    init_project(state.root)


@cli.command()
@click.option("--session", default=None, help="Session id or transcript path (hashed) to view")
@click.option("--tail", type=int, default=None, help="Show only the last N lines")
def logs(session: str | None, tail: int | None) -> None:
    """View a recent captain-hook session log."""
    show_logs(session=session, tail=tail)


@cli.group()
def skills() -> None:
    """Manage the bundled Claude Code skills."""


@skills.command(name="install")
@click.option("--force", is_flag=True, default=False, help="Replace skills that already exist in .claude/skills")
@click.pass_obj
def skills_install(state: CliState, force: bool) -> None:
    """Copy the bundled skills into .claude/skills/."""
    for name, status in install_skills(state.root, force=force).items():
        click.echo(f"  {status} {name}")


main = cli
