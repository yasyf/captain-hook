from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from captain_hook.hook_lint import Finding, copy_violations, lint_paths, lint_source, result_violations
from captain_hook.types import Action, HookResult
from tests.helpers import run_cli, write_hook

CLEAN_HOOK = """
from captain_hook import Allow, Input, Warn, warn_command

warn_command(
    ["pip", "install"],
    message="This repo installs with uv. Run `uv add <pkg>`.",
    tests={Input(command="pip install requests"): Warn(pattern="uv add"), Input(command="uv add requests"): Allow()},
)
"""

NOISY_HOOK = """
from captain_hook import Allow, Input, Warn, warn_command

warn_command(
    ["pip", "install"],
    message="User feedback 2026-06-09: 'stop using pip, this is uv only'. Run `uv add <pkg>`.",
    tests={Input(command="pip install requests"): Warn(pattern="uv add"), Input(command="uv add requests"): Allow()},
)
"""


def findings(source: str) -> list[tuple[str, str]]:
    return [(f.rule, f.detail) for f in lint_source(Path("hook.py"), textwrap.dedent(source))]


class TestCopyViolations:
    @pytest.mark.parametrize(
        "message",
        [
            "Raw `git push` bypasses the stack. Run `ccx vcs ship` instead.",
            "Tests must pass before you stop. Run `uv run pytest`, e.g. on the touched package.",
            "A module constant used once belongs inline at its call site. Inline {violations}.",
            'Never force-push `dev`. Run `git push --force-with-lease origin "$BRANCH"` on your lane branch.',
        ],
    )
    def test_rule_then_remediation_is_clean(self, message: str) -> None:
        assert copy_violations(message) == []

    def test_third_sentence_is_flagged(self) -> None:
        assert copy_violations("Do not do X. Do Y. This matters because Z.") == [
            "3 sentences; state the rule, then the remediation"
        ]

    def test_bullet_lines_count_as_sentences(self) -> None:
        assert copy_violations("Fix these:\n- one\n- two")[0].startswith("3 sentences")

    def test_length_cap(self) -> None:
        assert f"{301} chars; keep it to 300" in copy_violations("x" * 300 + ".")

    @pytest.mark.parametrize(
        ("message", "rule"),
        [
            ("User feedback 2026-09-17: 'and kill the finds'. Use rg.", "provenance"),
            ('Use rg, as the user said: "never use find here".', "quoted text"),
            ("Per R624, run ccx.", "record id"),
            ("Same failure as #24918. Retarget first.", "record id"),
            ("The queue merged it at 00:17:41Z. Recheck.", "date or time"),
            ("Owner order 2026-10-01. Use ccx.", "date or time"),
            ("Session 3f2a9c1e-1111-2222-3333-444455556666 stalled. Resume it.", "session id"),
            ("Landed in 37768ef4. Rebase.", "commit hash"),
            ("You have 120k tokens left. Compact.", "token count"),
            ("First seen on the merge queue. Retry.", "narrative"),
            ("The owner has not replied. Ask again.", "narrative"),
        ],
    )
    def test_context_noise_is_flagged(self, message: str, rule: str) -> None:
        assert any(violation.startswith(rule) for violation in copy_violations(message))

    @pytest.mark.parametrize(
        "message", ["Tombstone comment: {reasoning}", "Why: {r.reasoning}", "You said {evt.user_prompt}"]
    )
    def test_echoed_input_is_flagged(self, message: str) -> None:
        assert "echoes the prompt or the judge's reasoning; state the rule instead" in copy_violations(message)

    def test_code_spans_are_exempt(self) -> None:
        assert copy_violations("Commit with a subject. Run `git commit -m 'fix the thing'` on #12 at 2026-01-01.")
        assert not copy_violations("Commit with a subject. Run `git commit -m 'fix the thing' && gh pr view 1234`.")

    def test_result_violations_grade_message_and_note(self) -> None:
        assert result_violations(HookResult(action=Action.warn, message="Fine. Run `x`.")) == []
        assert result_violations(HookResult(action=Action.rewrite, note="One. Two. Three.")) == [
            "3 sentences; state the rule, then the remediation"
        ]


class TestStaticLint:
    def test_message_keywords_and_positional_messages_are_graded(self) -> None:
        assert findings(
            """
            from captain_hook import block_command, nudge, on

            nudge("One. Two. Three.")
            block_command(["git", "stash"], reason="Stash is shared", hint="Ask who said 'never stash anything here'")

            @on(Event.PreToolUse)
            def guard(evt):
                return evt.block(f"Blocked at 12:01 PT. Retry {evt.cmd.raw}.")
            """
        ) == [
            ("copy", "3 sentences; state the rule, then the remediation"),
            ("copy", "quoted text \"'never stash anything here'\""),
            ("copy", "date or time '12:01 PT'"),
        ]

    def test_module_constants_and_concatenation_resolve(self) -> None:
        assert findings(
            """
            WHY = "This happened on the queue. "
            MESSAGE = WHY + "Rebase first. Then push."
            gate(MESSAGE)
            """
        ) == [
            ("copy", "3 sentences; state the rule, then the remediation"),
            ("copy", "narrative 'This happened'"),
        ]

    def test_llm_message_lambda_echoing_reasoning(self) -> None:
        assert findings('llm_nudge("p", message=lambda r: f"Excuse detected: {r.reasoning}")') == [
            ("copy", "echoes the prompt or the judge's reasoning; state the rule instead")
        ]

    @pytest.mark.parametrize(
        ("source", "detail"),
        [
            ("re.search(r'git push', evt.command.raw)", "regexes command text"),
            ("re.match(r'gh', evt.tool_input['command'])", "regexes command text"),
            ("evt.cmd.raw.split()", "regexes command text"),
            ("shlex.split(line)", "splits a command by hand"),
            ("open(evt.transcript_path)", "reads the transcript by hand"),
            ("Path(home).glob('*.jsonl')", "reads transcript files by hand"),
            ("try:\n    run()\nexcept Exception:\n    pass", "swallows failures"),
            ("try:\n    run()\nexcept:\n    pass", "swallows failures"),
            ("with suppress(Exception):\n    run()", "swallows failures"),
            ("hook(Event.PreToolUse, only_if=[CommandCondition(r'git\\s+push')])", "matches a command name as text"),
            ("block_command(r'gh\\s+pr\\s+merge', reason='r')", "matches a command name as text"),
        ],
    )
    def test_hand_rolled_code_is_flagged(self, source: str, detail: str) -> None:
        assert [rule for rule, text in findings(source) if text.startswith(detail)] == ["code"]

    @pytest.mark.parametrize(
        "source",
        [
            "re.search(r'Traceback', evt.error)",
            "evt.command.q.runs('git', 'push')",
            "block_command(['git', 'stash'], reason='Stash is shared across worktrees')",
            "hook(Event.PreToolUse, only_if=[CommandCondition(r'curl.*\\|\\s*sh')])",
            "try:\n    run()\nexcept OSError:\n    pass",
        ],
    )
    def test_declarative_code_is_clean(self, source: str) -> None:
        assert findings(source) == []

    def test_comments_except_todos_and_workarounds(self) -> None:
        assert findings(
            """
            #!/usr/bin/env python
            # explains the obvious
            x = 1  # TODO: drop once upstream ships
            y = 2  # WORKAROUND: CC drops the field on resume
            z = 3  # noqa: E501
            """
        ) == [("comment", "rename or restructure instead of commenting")]


class TestLintPaths:
    def test_walks_directories_and_skips_tests(self, tmp_path: Path) -> None:
        (tmp_path / "hook.py").write_text("# narration\n")
        (tmp_path / "test_hook.py").write_text("# narration\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "helpers.py").write_text("# narration\n")
        (tmp_path / "conftest.py").write_text("# narration\n")
        assert lint_paths([tmp_path]) == [
            Finding(tmp_path / "hook.py", 1, "comment", "rename or restructure instead of commenting")
        ]


@pytest.fixture
def hooks_dir(tmp_path: Path) -> Path:
    (d := tmp_path / "hooks").mkdir()
    (d / "__init__.py").write_text("")
    return d


class TestCli:
    def test_lint_clean_hooks_exit_zero(self, hooks_dir: Path) -> None:
        write_hook(hooks_dir, CLEAN_HOOK)
        result = run_cli("lint", str(hooks_dir))
        assert (result.returncode, result.stdout) == (0, "")

    def test_lint_defaults_to_hooks_dir_and_reports_json(self, hooks_dir: Path) -> None:
        write_hook(hooks_dir, NOISY_HOOK)
        result = run_cli("lint", "--json", hooks_dir=str(hooks_dir))
        assert result.returncode == 1
        assert {json.loads(line)["detail"].split(" ")[0] for line in result.stdout.splitlines()} == {
            "provenance",
            "quoted",
            "date",
        }

    def test_test_command_fails_on_lint_findings(self, hooks_dir: Path) -> None:
        write_hook(hooks_dir, NOISY_HOOK)
        result = run_cli("test", hooks_dir=str(hooks_dir))
        assert result.returncode == 1
        assert "LINT" in result.stdout and "3 lint findings" in result.stdout

    def test_test_command_passes_clean_hooks(self, hooks_dir: Path) -> None:
        write_hook(hooks_dir, CLEAN_HOOK)
        result = run_cli("test", hooks_dir=str(hooks_dir))
        assert result.returncode == 0, result.stdout


class TestEscapeNotation:
    @pytest.mark.parametrize(
        "source",
        [
            "RAW = re.compile(r'#\\s*ccx:raw')",
            "skip = 'ccx:raw' in evt.command.raw",
            "skip = evt.command.raw.endswith('# root:raw')",
            "lane = prompt.partition('tooling-lane:')",
            "raw = os.environ.get('CAPT_HOOK_CCX_RAW')",
            "raw = os.environ['CAPT_HOOK_CCX_RAW']",
        ],
    )
    def test_hand_parsed_escape_is_flagged(self, source: str) -> None:
        assert ("code", "parses the ccx escape by hand; match it with Annotated(...)") in findings(source)

    def test_escape_in_messages_and_test_inputs_is_clean(self) -> None:
        assert not findings(
            """
            hook(
                Event.PreToolUse,
                only_if=[Runs("gt", "submit")],
                skip_if=[Annotated("raw")],
                message="Stack writes go through ccx. Run `ccx vcs stack submit`, or end the command with `# ccx:raw`.",
                block=True,
                tests={Input(command="gt submit  # ccx:raw"): Allow()},
            )
            """
        )

    @pytest.mark.parametrize(
        "source",
        [
            "hook(Event.PreToolUse, only_if=[Tool('Agent|Task')], message='m', block=True)",
            "@on(Event.PreToolUse, only_if=[Tool('Read')])\ndef guard(evt):\n    return evt.block('m')",
        ],
    )
    def test_unescaped_dispatch_or_read_block_is_flagged(self, source: str) -> None:
        assert [detail for rule, detail in findings(source) if rule == "code"] == [
            "blocks a dispatch or read with no escape; add skip_if=[Annotated(...)] or confirm=Confirm(...)"
        ]

    @pytest.mark.parametrize(
        "source",
        [
            "hook(Event.PreToolUse, only_if=[Tool('Agent')], skip_if=[Annotated('role')], message='m', block=True)",
            "hook(Event.PreToolUse, only_if=[Tool('Agent')], message='m', block=True, confirm=Confirm(rule='r'))",
            "hook(Event.PreToolUse, only_if=[Tool('Bash')], message='m', block=True)",
            "@on(Event.PreToolUse, only_if=[Tool('Task')])\n"
            "def guard(evt):\n"
            "    return None if 'role' in evt.annotations else evt.block('m')",
            "hook(Event.PreToolUse, only_if=[Tool('Agent')], message='m')",
            "@on(Event.PreToolUse, only_if=[Tool('Read')])\n"
            "def guard(evt):\n"
            "    return evt.block('m', confirm=Confirm(rule='r'))",
        ],
    )
    def test_escaped_or_unrelated_blocks_are_clean(self, source: str) -> None:
        assert not [detail for rule, detail in findings(source) if rule == "code"]
