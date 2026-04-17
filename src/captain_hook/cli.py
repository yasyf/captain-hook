from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from captain_hook.app import _state, discover_hooks, load_gitignore, reset
from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch
from captain_hook.log import setup_logging
from captain_hook.session import SessionStore, ensure_session
from captain_hook.transcript import Transcript
from captain_hook.types import Event


def generate_settings(hooks_dir: str = ".claude/hooks", from_source: str | None = None) -> dict[str, Any]:
    events_by_async: defaultdict[bool, set[str]] = defaultdict(set)
    for entry in _state.hooks:
        for member in Event:
            if member in entry.spec.events and (name := member.name):
                events_by_async[entry.spec.async_].add(name)

    from_flag = f" --from {from_source}" if from_source else ""

    def commands(event: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "command",
                "command": (
                    f"uvx{from_flag} captain-hook"
                    f" --hooks $CLAUDE_PROJECT_DIR/{hooks_dir}"
                    f" --root $CLAUDE_PROJECT_DIR"
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


def generate_settings_json(hooks_dir: str = ".claude/hooks", from_source: str | None = None) -> str:
    return json.dumps(generate_settings(hooks_dir, from_source=from_source), indent=2)


def merge_settings(hooks_dir: str, settings_path: Path, from_source: str | None = None) -> dict[str, Any]:
    hook_settings = generate_settings(hooks_dir, from_source=from_source)
    if settings_path.exists():
        existing = json.loads(settings_path.read_text())
        existing["hooks"] = hook_settings["hooks"]
        return existing
    return hook_settings


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
    transcript = Transcript.from_path(resolved_path) if resolved_path else None

    session_dir = ensure_session(transcript_path) if transcript_path else ensure_session(root or Path.cwd())
    ctx = HookContext(
        session=SessionStore(session_dir),
        transcript=transcript,
        settings=_state.settings,
    )
    evt = event.event_class(_raw=raw, ctx=ctx)

    if output := dispatch(event, evt, session_dir=session_dir, async_=async_):
        print(json.dumps(output))


EXAMPLE_HOOK = '''\
from captain_hook import Event, Tool, nudge, block_command

block_command(
    r"rm\\s+-rf\\s+/",
    reason="Refusing to run rm -rf /",
)

nudge(
    "Remember to run tests before committing.",
    only_if=[Tool("Bash")],
    events=Event.PostToolUse,
    max_fires=1,
)
'''

def init_project(root: Path) -> None:
    hooks_dir = root / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    example = hooks_dir / "example.py"
    if not example.exists():
        example.write_text(EXAMPLE_HOOK)
        print(f"  Created {example.relative_to(root)}")

    settings_path = root / ".claude" / "settings.local.json"
    reset()
    discover_hooks(str(hooks_dir))
    merged = merge_settings(".claude/hooks", settings_path)
    settings_path.write_text(json.dumps(merged, indent=2) + "\n")
    print(f"  Updated {settings_path.relative_to(root)}")

    print("\nDone! Write hooks in .claude/hooks/, then run: captain-hook test")


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
    settings_parser.add_argument("--hooks-dir", default=".claude/hooks", help="Hooks directory relative to project root")
    settings_parser.add_argument("--no-merge", action="store_true", help="Output standalone JSON instead of merging")
    settings_parser.add_argument("--from", dest="from_source", default=None, help="Package source for uvx --from (local path or PyPI spec)")

    sub.add_parser("test", help="Run inline tests from all registered hooks")
    sub.add_parser("init", help="Scaffold hooks directory, bin script, and settings")

    return parser


def run_tests() -> None:
    from captain_hook.testing.helpers import run_inline_tests

    results = run_inline_tests()
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

    root = Path(args.root) if args.root else Path.cwd()

    if args.command == "init":
        init_project(root)
        return

    reset()
    load_gitignore(root)
    discover_hooks(args.hooks)

    match args.command:
        case "run":
            run_event(args.event, async_=args.async_, root=root)
        case "generate-settings":
            if args.no_merge:
                print(generate_settings_json(args.hooks_dir, from_source=args.from_source))
            else:
                settings_path = root / ".claude" / "settings.local.json"
                merged = merge_settings(args.hooks_dir, settings_path, from_source=args.from_source)
                print(json.dumps(merged, indent=2))
        case "test":
            run_tests()
        case _:
            parser.error(f"Unknown command: {args.command}")
