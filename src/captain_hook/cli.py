from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from captain_hook._loader import discover_hooks
from captain_hook.app import _state, load_gitignore, reset
from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch
from captain_hook.log import setup_logging
from captain_hook.session import SessionStore, ensure_session
from captain_hook.transcript import Transcript
from captain_hook.types import Event

EXAMPLE_HOOK = '''\
from __future__ import annotations

import re

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    Event,
    HookResult,
    InlineTests,
    Input,
    Prompt,
    Signal,
    Signals,
    SourceEdits,
    audit,
    block_command,
    nudge,
    on,
    prompt_check,
)

# 1. block_command — refuse a dangerous shell command before it runs.
# see docs/guide/primitives.md
RM_RF_TESTS: InlineTests = {
    Input(tool="Bash", command="rm -rf /"): Block(pattern="unrecoverable"),
    Input(tool="Bash", command="rm -rf build/"): Block(pattern="unrecoverable"),
    Input(tool="Bash", command="rm notes.txt"): Allow(),
}
block_command(
    ["rm", "-rf", "*"],
    reason="rm -rf is unrecoverable on this machine",
    hint="Delete files individually or move them to a trash dir",
    tests=RM_RF_TESTS,
)

# 2. nudge — fire on Stop when retry-language piles up in the transcript.
# see docs/guide/primitives.md and docs/examples/failure-recovery.md
RETRY_SIGNALS = Signals(
    patterns=[
        Signal(pattern=r"let me try again", weight=2, flags=re.IGNORECASE),
        Signal(pattern=r"same (error|failure|issue)", weight=2, flags=re.IGNORECASE),
        Signal(pattern=r"\\bretry(ing)?\\b", weight=1, flags=re.IGNORECASE),
    ],
    threshold=3,
    window=10,
)
nudge(
    "Repeated retries detected. Read DEBUGGING.md or invoke /codex before the next attempt.",
    signals=RETRY_SIGNALS,
    events=Event.Stop,
    max_fires=1,
)

# 3. audit — append one JSONL record per matching tool call into .context/.
# see docs/guide/primitives.md
audit(
    Event.PreToolUse | Event.PostToolUse | Event.Stop,
    log_dir=".context/hook-audit",
)

# 4. prompt_check — LLM gate on Python edits that smell like unfinished work.
# see docs/guide/primitives.md and docs/examples/test-integrity.md
PLACEHOLDER_TEMPLATE = """
The agent just edited {fp}. Flag the change if the new content contains
placeholder text the agent forgot to replace — `TODO: replace`, `FILLME`,
`<your-...-here>`, `pass  # implement`, etc.

--- new ---
{new}
"""


@on(Event.PostToolUse, only_if=[SourceEdits(lang="py")])
def warn_on_placeholder(evt: BaseHookEvent) -> HookResult | None:
    if not (fp := evt.file) or not (new := evt.content):
        return None
    if not re.search(r"TODO:?\\s*replace|FILLME|<your-.+?-here>", new, re.IGNORECASE):
        return None
    return prompt_check(
        evt,
        Prompt.from_template(PLACEHOLDER_TEMPLATE, fp=fp.path, new=new),
        prefix="PLACEHOLDER LEFT IN CODE",
    )
'''


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


def is_captain_hook_group(group: dict[str, Any]) -> bool:
    return any("captain-hook" in (h.get("command") or "") for h in group.get("hooks") or [])


def merge_init_settings(
    hooks_dir: str, settings_path: Path, from_source: str | None = None
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
        example.write_text(EXAMPLE_HOOK)

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
    print("  3. captain-hook test --hooks .claude/hooks       # verify inline tests")
    print("  4. captain-hook generate-settings --hooks ...    # rewire after adding events")


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

    test_parser = sub.add_parser("test", help="Run inline tests from all registered hooks")
    test_parser.add_argument("--json", dest="json_output", action="store_true", help="Emit one JSON record per test (CI mode)")
    sub.add_parser("init", help="Scaffold hooks directory, bin script, and settings")

    return parser


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
            run_tests(json_output=args.json_output)
        case _:
            parser.error(f"Unknown command: {args.command}")
