from __future__ import annotations

import importlib.resources
import json
import os
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


def generate_settings(hooks_dir: str = ".claude/hooks", from_source: str = DIST_NAME) -> dict[str, Any]:
    events_by_async: defaultdict[bool, set[str]] = defaultdict(set)
    for entry in _state.hooks:
        for member in Event:
            if member in entry.spec.events and (name := member.name):
                events_by_async[entry.spec.async_].add(name)

    from_flag = "" if from_source == DIST_NAME else f" --from {from_source}"
    hooks_flag = "" if hooks_dir == ".claude/hooks" else f" --hooks $CLAUDE_PROJECT_DIR/{hooks_dir}"

    def commands(event: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "command",
                "command": (
                    f"uvx{from_flag} capt-hook"
                    f"{hooks_flag}"
                    f" run {event}"
                    f"{' --async' if is_async else ''}"
                ),
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

    if output := dispatch(event, evt, session_dir=session_dir, async_=async_):
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
    print("Next:")
    print("  1. Read the quickstart: docs/getting-started/quickstart.md")
    print("  2. Edit example.py or add new files under .claude/hooks/")
    print("  3. capt-hook test       # verify inline tests")
    print("  4. capt-hook generate-settings    # rewire after adding events")


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
            print(json.dumps({
                "id": name,
                "status": status,
                "expected": expected_by_id.get(name, ""),
                "reason": detail,
            }))
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
    help=(
        "Dispatch a hook event (reads JSON from stdin, writes JSON to stdout).\n\n"
        f"EVENT is one of: {EVENT_NAMES}."
    ),
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
    """Scaffold hooks directory, bin script, and settings."""
    init_project(state.root)


@cli.command()
@click.option("--session", default=None, help="Session id or transcript path (hashed) to view")
@click.option("--tail", type=int, default=None, help="Show only the last N lines")
def logs(session: str | None, tail: int | None) -> None:
    """View a recent captain-hook session log."""
    show_logs(session=session, tail=tail)


main = cli
