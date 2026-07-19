from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from functools import reduce
from pathlib import Path
from shutil import copyfile
from typing import get_args, get_type_hints

import captain_hook
from captain_hook import Event, File, TCondition
from captain_hook.style import matchers, styleguide
from captain_hook.style.matchers import Matcher

ROOT = Path(__file__).parents[1]
API_REFERENCE = ROOT / "captain_hook/skills/authoring-hooks/references/capt-hook-api.md"
MATCHER_REFERENCE = ROOT / "captain_hook/skills/translating-styleguides/references/matcher-reference.md"
REGEN_COMMAND = "uv run python -m tests.test_skill_docs"

EVENT_DESCRIPTIONS: dict[Event, tuple[str, str]] = {
    Event.PreToolUse: ("Before a tool runs", "Block dangerous commands"),
    Event.PermissionRequest: (
        "A permission dialog would be shown",
        "Auto-answer dialogs (allow/deny/rewrite); no decision means the dialog shows",
    ),
    Event.PostToolUse: ("After a tool succeeds", "Lint output, nudge conventions"),
    Event.PostToolUseFailure: ("After a tool fails", "Suggest debugging steps"),
    Event.UserPromptSubmit: ("User sends a message", "Detect request patterns"),
    Event.Stop: ("Agent is about to stop", "Gate on test execution"),
    Event.SubagentStop: ("A subagent finishes", "Verify subagent work"),
    Event.SubagentStart: ("A subagent launches", "Capture initial state"),
    Event.Notification: ("Informational event", "Logging, metrics"),
    Event.PreCompact: ("Before context compaction", "Preserve critical context"),
    Event.SessionStart: (
        "Session starts, resumes, clears, or compacts (`evt.source`)",
        "Provision resources, prime state",
    ),
    Event.SessionEnd: ("Session ends", "Cleanup, audit logging"),
}

PRIMITIVES: dict[str, Callable[..., object]] = {
    "block_command": captain_hook.block_command,
    "warn_command": captain_hook.warn_command,
    "rewrite_command": captain_hook.rewrite_command,
    "set_tool_input": captain_hook.set_tool_input,
    "gate": captain_hook.gate,
    "nudge": captain_hook.nudge,
    "lint": captain_hook.lint,
    "workflow": captain_hook.workflow,
    "install_binary": captain_hook.install_binary,
    "llm_gate": captain_hook.llm_gate,
    "llm_nudge": captain_hook.llm_nudge,
    "prompt_check": captain_hook.prompt_check,
    "styleguide": styleguide,
    "approve": captain_hook.approve,
    "deny": captain_hook.deny,
    "llm_approve": captain_hook.llm_approve,
}

PRIMITIVE_DESCRIPTIONS = {
    "block_command": '`PreToolUse` + `Tool("Bash")`; message `"BLOCKED: {reason}. {hint}."`',
    "warn_command": "warns, never blocks",
    "rewrite_command": (
        '`PreToolUse` + `Tool("Bash")`; a pattern with an ast-grep metavar (`cat $$$ARGS`) rewrites structurally '
        "via `ast_grep.rewrite`, otherwise `re.sub(pattern, replace, command)`; allows with the rewritten command"
    ),
    "set_tool_input": (
        "`PreToolUse` + `Tool(tool)`; fills a **missing** top-level input field with `value` and allows, never "
        "clobbering a present one"
    ),
    "gate": (
        "`Stop \\| SubagentStop`; blocks, defaults to **unlimited** fires (keeps enforcing); `skip_if` is additive "
        "with an automatic `Waiting()`"
    ),
    "nudge": (
        "`PostToolUse` (with signals) else `PreToolUse`; default fires 3 / 1; `when` vetoes even with `signals`; warns"
    ),
    "lint": (
        '`PostToolUse`, `Tool("Edit\\|Write")` + the `lang` globs, skips test files; `trigger` pre-filters string '
        "**and** ast checks"
    ),
    "workflow": "guard on `SubagentStop`, `max_fires=1`",
    "install_binary": (
        "`SessionStart`, async; runs `script` via `/bin/sh` from the calling pack file's dir; always allows"
    ),
    "llm_gate": (
        "`Stop \\| SubagentStop`; defaults to **unlimited** fires (keeps enforcing); blocks when `verdict(result)` "
        "— default `GateVerdict.block`"
    ),
    "llm_nudge": ("`PostToolUse`, `max_fires=3`; warns when `verdict(result)` — default `NudgeVerdict.fire`"),
    "prompt_check": ("call inside an `@on` handler; returns `HookResult \\| None` from `PromptCheckVerdict`"),
    "styleguide": "AST style rules — owned by the `translating-styleguides` skill",
    "approve": (
        "`PreToolUse \\| PermissionRequest`; pre-authorizes matching tools before the prompt and answers matching "
        "dialogs with allow; **no fire cap**. Unconditioned == a permanent `--dangerously-skip-permissions`; always "
        "scope with conditions"
    ),
    "deny": (
        "`PreToolUse \\| PermissionRequest`; blocks matching tools before the prompt and answers matching dialogs "
        "with deny, `reason` shown to the user; no fire cap. Unconditioned bricks every tool"
    ),
    "llm_approve": (
        "`PermissionRequest`; LLM safety judge seeded from `claude auto-mode defaults` (+ your `rubric`); a safe "
        "verdict allows, an unsafe verdict or LLM failure returns `None` so the dialog shows, never an auto-deny. "
        "One LLM round-trip per matching ask"
    ),
}

CONDITION_DESCRIPTIONS: dict[tuple[str, ...], tuple[str, str]] = {
    ("Tool",): (
        "Filter by tool name",
        '`Tool("Bash")` or `Tool("Edit", "Write")` — exact names (not regex), aliases auto-expand (Bash=Execute, '
        "Write=Create, Agent=Task), MCP suffixes match",
    ),
    ("FilePath",): ("Filter by file path", '`FilePath("*.py", "*.pyi")`'),
    ("Command",): (
        "Filter by bash command text",
        '`CommandCondition(r"git\\s+push")` (`captain_hook.types.Command`) — regex over the raw line and each parsed '
        "command",
    ),
    ("Content",): (
        "Filter by file content being written",
        '`Content(r"print\\(")` (multiline regex over Edit new / Write content)',
    ),
    ("ToolInput",): (
        "Filter by raw tool-input fields",
        '`ToolInput(model=r"(?i)\\bhaiku\\b")` (kwargs AND across fields; scalar values coerced to text)',
    ),
    ("WorkflowScript",): (
        "Filter by a Workflow script",
        '`WorkflowScript(model="haiku")` — any `agent()` opt as a kwarg (`effort=`, `agentType=`, …), all AND',
    ),
    ("Pattern",): (
        "Match edit content by code shape (ast-grep)",
        '`Pattern("os.system($CMD)")` — structural, ignores matches inside strings/comments; `lang` inferred from the '
        "edited file's extension",
    ),
    ("Agent",): (
        "Filter by subagent type",
        '`Agent("cleanup")` or `Agent("Explore", "claude-code-guide")`',
    ),
    ("FromSubagent",): (
        "Event comes from a subagent/teammate",
        "`FromSubagent()` — the payload carries an `agent_id`; matches the ask's *origin*, where `Agent` matches its "
        "*type*",
    ),
    ("SkipPermissions",): (
        "Session launched with bypass available",
        "`SkipPermissions()` — walks to the nearest `claude` ancestor process and matches "
        "`--dangerously-skip-permissions` **or** `--allow-dangerously-skip-permissions`; availability counts as "
        "consent, whatever the active `permission_mode`",
    ),
    ("UsedSkill",): (
        "Skill was invoked",
        '`UsedSkill("codex")` — bare name also matches `plugin:name`',
    ),
    ("ReadFile",): (
        "File was previously read",
        '`ReadFile("TESTING.md")` — fnmatch globs; anchor dirs with `**/`',
    ),
    ("TestFile",): ("Match only test files", "`TestFile()` ({patterns})"),
    ("SourceEdits",): (
        "Python source edits (skips tests by default, in-repo only)",
        '`SourceEdits(lang="py")`; `lang` also `ts`, `go`, `rs`, ...; `project_only=False` to also match '
        "out-of-repo files",
    ),
    ("TouchedFile",): ("File was previously edited", '`TouchedFile("**/*.py")`'),
    ("RanCommand",): (
        "Command was previously run",
        '`RanCommand("uv", "run", "pytest")` — argv-prefix tokens, wrapper-transparent (`sudo`/`env`/`timeout` '
        "stripped) but launcher-literal (`uv run pytest` ≠ `pytest`; list each spelling as its own entry)",
    ),
    ("Runs",): (
        "Bash argv prefix (structural, no false positives)",
        '`Runs("git", "stash")` — matches `git stash [...]`, not `echo git stash`',
    ),
    ("InPlanMode",): ("During plan mode", "`InPlanMode()`"),
    ("Waiting",): (
        "Session is parked on background work",
        "`Waiting()` — background shells/subagents/workflows in flight, or an undelivered task notification; typically "
        "`skip_if=[Waiting()]` on Stop gates",
    ),
    ("Or", "And", "Not"): ("Combine across types", "`Or(...)`, `And(...)`, `Not(...)`"),
    ("CustomCondition",): ("Custom logic", "implement `CustomCondition`"),
}

MATCHER_CONSTANT_DESCRIPTIONS = {
    "module": "`ast.Module` (the file root)",
    "cls": "a class definition",
    "func": "a sync or async function definition",
    "definition": "`M.cls \\| M.func`",
    "imports": "`import x` / `from x import y`",
    "call": "any call expression",
    "assignment": "`x = ...` / `x: T = ...`",
    "control_flow": "`if` / `for` / `while` / `with` / `try` / `except` (incl. async)",
    "type_checking": "an `if TYPE_CHECKING:` block",
    "future_annotations": "a module containing `from __future__ import annotations`",
    "forward_ref": "a quoted (string) type reference inside an annotation",
    "private": "a definition/assignment/parameter named `_x` (single leading underscore)",
    "dunder": "one named `__x__`",
    "constant": "one named `UPPER_SNAKE` (optional leading underscore)",
}

MATCHER_FACTORY_DESCRIPTIONS = {
    "kind": (
        "`M.kind(*types, label=None)`",
        "any of the given `ast` node types — the primitive for a category not shipped, e.g. `M.kind(ast.Lambda)`",
    ),
    "calls": ("`M.calls(name)`", 'a call to the bare-name function `name`, e.g. `M.calls("zip")`'),
    "kwarg": ("`M.kwarg(name)`", "a call passing keyword argument `name` — combine with `M.calls`"),
    "ref": ("`M.ref(name)`", 'a bare name reference, e.g. `M.ref("Any")`'),
    "named": (
        "`M.named(pattern)`",
        "a class/function/assignment/parameter whose bound name matches the regex (`re.search`)",
    ),
    "annotated": (
        "`M.annotated(inner=None)`",
        "an annotation owner (annotated variable, parameter, or return; excludes `*args`/`**kwargs`); with `inner`, "
        'its annotation expression must also match, e.g. `M.annotated(M.ref("Any"))`',
    ),
}

MATCHER_STRUCTURE_DESCRIPTIONS = {
    "under": ("`M.under(m)`", "a node with *any ancestor* matching `m`"),
    "child_of": ("`M.child_of(m)`", "a node whose *immediate parent* matches `m`"),
    "following": ("`M.following(m)`", "a body statement that comes *after the first sibling* matching `m`"),
}

STRUCTURE = {"under", "child_of", "following"}


class DefaultDisplay:
    text: str

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


def exact(live: set[object], documented: set[object], label: str) -> None:
    assert live == documented, (
        f"{label} descriptions differ from live objects: "
        f"missing={sorted(map(str, live - documented))}, orphaned={sorted(map(str, documented - live))}"
    )


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    return "\n".join(
        [
            f"| {' | '.join(headers)} |",
            f"|{'|'.join('---' for _ in headers)}|",
            *(f"| {' | '.join(row)} |" for row in rows),
        ]
    )


def display_default(value: object) -> str:
    match value:
        case Event():
            return " | ".join(f"Event.{member.name}" for member in Event if member in value)
        case type():
            return value.__name__
        case _ if callable(value):
            return "…"
        case _:
            return repr(value)


def display_signature(primitive: Callable[..., object]) -> str:
    signature = inspect.signature(primitive)
    parameters = [
        parameter.replace(
            annotation=inspect.Parameter.empty,
            default=(
                inspect.Parameter.empty
                if parameter.default is inspect.Parameter.empty
                else DefaultDisplay(display_default(parameter.default))
            ),
        )
        for parameter in signature.parameters.values()
    ]
    return str(signature.replace(parameters=parameters, return_annotation=inspect.Signature.empty)).replace("|", "\\|")


def render_events() -> str:
    events = tuple(Event)
    exact(set(events), set(EVENT_DESCRIPTIONS), "Event")
    return table(
        ("Event", "When it fires", "Typical use"),
        [(f"`{event.name}`", *EVENT_DESCRIPTIONS[event]) for event in events],
    )


def render_primitives() -> str:
    exact(set(PRIMITIVES), set(PRIMITIVE_DESCRIPTIONS), "primitive")
    return table(
        ("Primitive", "Signature (keyword-only after `*`)", "Defaults"),
        [
            (f"`{name}`", f"`{display_signature(primitive)}`", PRIMITIVE_DESCRIPTIONS[name])
            for name, primitive in PRIMITIVES.items()
        ],
    )


def condition_usage(names: tuple[str, ...], usage: str) -> str:
    if names != ("TestFile",):
        return usage
    return usage.format(patterns=", ".join(f"`{pattern.replace('|', r'\|')}`" for pattern in File.TEST_PATTERNS))


def render_conditions() -> str:
    live_names = tuple(condition.__name__ for condition in get_args(TCondition))
    documented_names = {name for names in CONDITION_DESCRIPTIONS for name in names}
    exact(set(live_names), documented_names, "condition")
    groups = {name: names for names in CONDITION_DESCRIPTIONS for name in names}
    ordered_groups = tuple(dict.fromkeys(groups[name] for name in live_names))
    return table(
        ("Need", "Use"),
        [
            (
                CONDITION_DESCRIPTIONS[names][0],
                condition_usage(names, CONDITION_DESCRIPTIONS[names][1]),
            )
            for names in ordered_groups
        ],
    )


def matcher_constants() -> dict[str, Matcher]:
    return {name: value for name, value in vars(matchers).items() if isinstance(value, Matcher)}


def matcher_factories() -> dict[str, Callable[..., Matcher]]:
    return {
        name: value
        for name, value in vars(matchers).items()
        if not name.startswith("_") and inspect.isfunction(value) and get_type_hints(value).get("return") is Matcher
    }


def classified_matcher_factories() -> dict[str, Callable[..., Matcher]]:
    factories = matcher_factories()
    documented = set(MATCHER_FACTORY_DESCRIPTIONS) | set(MATCHER_STRUCTURE_DESCRIPTIONS)
    exact(set(factories), documented, "matcher factory")
    exact(STRUCTURE, set(MATCHER_STRUCTURE_DESCRIPTIONS), "structural matcher factory")
    exact(set(factories) - STRUCTURE, set(MATCHER_FACTORY_DESCRIPTIONS), "ordinary matcher factory")
    return factories


def render_matcher_constants() -> str:
    constants = matcher_constants()
    exact(set(constants), set(MATCHER_CONSTANT_DESCRIPTIONS), "matcher constant")
    return table(
        ("Constant", "Matches"),
        [(f"`M.{name}`", MATCHER_CONSTANT_DESCRIPTIONS[name]) for name in constants],
    )


def render_matcher_factories() -> str:
    factories = classified_matcher_factories()
    return table(
        ("Factory", "Matches"),
        [MATCHER_FACTORY_DESCRIPTIONS[name] for name in factories if name not in STRUCTURE],
    )


def render_matcher_structure() -> str:
    factories = classified_matcher_factories()
    return table(
        ("Factory", "Matches"),
        [MATCHER_STRUCTURE_DESCRIPTIONS[name] for name in factories if name in STRUCTURE],
    )


DOCUMENTS: dict[Path, Mapping[str, Callable[[], str]]] = {
    API_REFERENCE: {
        "events": render_events,
        "primitives": render_primitives,
        "conditions": render_conditions,
    },
    MATCHER_REFERENCE: {
        "matcher-constants": render_matcher_constants,
        "matcher-factories": render_matcher_factories,
        "matcher-structure": render_matcher_structure,
    },
}


def replace_generated(document: str, name: str, interior: str) -> str:
    opening = f"<!-- gen:{name} -->"
    closing = f"<!-- /gen:{name} -->"
    assert document.count(opening) == document.count(closing) == 1, f"expected exactly one {name} marker block"
    before, _, remainder = document.partition(opening)
    _, _, after = remainder.partition(closing)
    return f"{before}{opening}\n{interior}\n{closing}{after}"


def rendered_document(path: Path, renderers: Mapping[str, Callable[[], str]]) -> str:
    return reduce(
        lambda document, item: replace_generated(document, item[0], item[1]()),
        renderers.items(),
        path.read_text(),
    )


def regenerate_file(path: Path, renderers: Mapping[str, Callable[[], str]]) -> None:
    path.write_text(rendered_document(path, renderers))


def regenerate() -> None:
    for path, renderers in DOCUMENTS.items():
        regenerate_file(path, renderers)


def test_display_default() -> None:
    assert display_default(lambda: None) == "…"
    assert display_default(str) == "str"
    assert display_default(Event.PreToolUse | Event.PermissionRequest) == ("Event.PreToolUse | Event.PermissionRequest")
    assert display_default(None) == "None"


def test_generated_skill_docs(tmp_path: Path) -> None:
    for source, renderers in DOCUMENTS.items():
        copyfile(source, target := tmp_path / source.name)
        regenerate_file(target, renderers)
        assert target.read_bytes() == source.read_bytes(), f"{source.relative_to(ROOT)} is stale; run `{REGEN_COMMAND}`"


def test_regeneration_is_idempotent(tmp_path: Path) -> None:
    for source, renderers in DOCUMENTS.items():
        copyfile(source, target := tmp_path / source.name)
        regenerate_file(target, renderers)
        first = target.read_bytes()
        regenerate_file(target, renderers)
        assert target.read_bytes() == first


if __name__ == "__main__":
    regenerate()
