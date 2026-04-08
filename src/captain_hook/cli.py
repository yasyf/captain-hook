from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from captain_hook.app import HookApp
from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch
from captain_hook.log import setup_logging
from captain_hook.session import SessionStore, ensure_session
from captain_hook.transcript import Transcript
from captain_hook.types import Event


def generate_settings(app: HookApp, run_command: str) -> dict[str, Any]:
    """Generate a Claude Code settings dict mapping events to hook runner commands.

    Args:
        app: The HookApp with registered hooks.
        run_command: Path to the hooks runner script.

    Returns:
        Settings dict suitable for ``.claude/settings.local.json``.
    """
    events_by_async: defaultdict[bool, set[str]] = defaultdict(set)
    for entry in app.hooks:
        for member in Event:
            if member in entry.spec.events and (name := member.name):
                events_by_async[entry.spec.async_].add(name)

    def commands(event: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "command",
                "command": f"$CLAUDE_PROJECT_DIR/{run_command} run {event}{' --async' if is_async else ''}",
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


def generate_settings_json(app: HookApp, run_command: str) -> str:
    """Generate Claude Code settings as a formatted JSON string.

    Args:
        app: The HookApp with registered hooks.
        run_command: Path to the hooks runner script.

    Returns:
        Pretty-printed JSON string.
    """
    return json.dumps(generate_settings(app, run_command), indent=2)


def run_event(
    app: HookApp,
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
    transcript = Transcript.from_path(resolved_path) if resolved_path else None

    session_dir = ensure_session(transcript_path) if transcript_path else ensure_session(root or Path.cwd())
    ctx = HookContext(
        session=SessionStore(session_dir),
        transcript=transcript,
        settings=app.settings,
    )
    evt = event.event_class(_raw=raw, ctx=ctx)

    if output := dispatch(app, event, evt, session_dir=session_dir, async_=async_):
        print(json.dumps(output))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="captain-hook",
        description="Captain Hook — declarative hook framework for Claude Code lifecycle events.",
    )
    parser.add_argument("--hooks", default="src", help="Path to hooks package directory (default: src)")
    parser.add_argument("--root", default=None, help="Project root for gitignore and session resolution")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Dispatch a hook event (reads JSON from stdin, writes JSON to stdout)")
    run_parser.add_argument("event", help=f"Event type: {', '.join(n for e in Event if (n := e.name))}")
    run_parser.add_argument("--async", dest="async_", action="store_true", default=False, help="Run async hooks only")

    settings_parser = sub.add_parser(
        "generate-settings", help="Generate Claude Code settings JSON for .claude/settings.local.json"
    )
    settings_parser.add_argument("--run-command", default=".claude/bin/captain-hook", help="Path to hooks runner script")

    sub.add_parser("test", help="Run inline tests from all registered hooks")

    return parser


def run_tests(app: HookApp) -> None:
    from captain_hook.testing.helpers import run_inline_tests

    results = run_inline_tests(app)
    if not results:
        print("No inline tests found.")
        return

    passed = failed = errors = skipped = 0
    for name, status, _ok, detail in results:
        match status:
            case "pass":
                passed += 1
                print(f"  PASS  {name}")
            case "skip":
                skipped += 1
                print(f"  SKIP  {name}: {detail}")
            case "fail":
                failed += 1
                print(f"  FAIL  {name}: {detail}")
            case "error":
                errors += 1
                print(f"  ERROR {name}: {detail}")
            case _:
                pass

    total = passed + failed + errors + skipped
    print(f"\n{total} tests: {passed} passed, {failed} failed, {errors} errors, {skipped} skipped")
    if failed or errors:
        sys.exit(1)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    app = HookApp()
    root = Path(args.root) if args.root else Path.cwd()
    app.load_gitignore(root)
    app.discover_hooks(args.hooks)

    match args.command:
        case "run":
            run_event(app, args.event, async_=args.async_, root=root)
        case "generate-settings":
            print(generate_settings_json(app, args.run_command))
        case "test":
            run_tests(app)
        case _:
            parser.error(f"Unknown command: {args.command}")
