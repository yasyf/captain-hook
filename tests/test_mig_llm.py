from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from captain_hook.app import HookApp, _current_app
from captain_hook.signals import score_signals
from captain_hook.types import Event, RanCommand, ReadFile, RegisteredHook, UsedSkill


def _reload_module(name: str) -> None:
    if name in sys.modules:
        importlib.reload(sys.modules[name])
    else:
        importlib.import_module(name)


@pytest.fixture
def app(tmp_path: Path) -> HookApp:
    a = HookApp()
    token = _current_app.set(a)
    yield a  # type: ignore[misc]
    _current_app.reset(token)
    a.reset()


def _find_gate_by_events_and_max_fires(
    hooks: list[RegisteredHook],
    events: Event,
    max_fires: int,
) -> RegisteredHook:
    matches = [h for h in hooks if h.spec.events == events and h.spec.max_fires == max_fires]
    assert len(matches) == 1, f"Expected 1 hook with events={events} max_fires={max_fires}, got {len(matches)}"
    return matches[0]


def _find_nudge(hooks: list[RegisteredHook]) -> RegisteredHook:
    matches = [h for h in hooks if "nudge" in h.name]
    assert len(matches) == 1, f"Expected 1 nudge hook, got {len(matches)}"
    return matches[0]


# ==================== VAL-MIG-LLM-001: Rationalization detector signals ====================


class TestMigLlmRationalizationSignals:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.llm_hooks")

    def test_mig_llm_rationalization_signal_zero_because_scores_high(self) -> None:
        from hooks.llm_hooks import RATIONALIZATION_SIGNALS

        score = score_signals(RATIONALIZATION_SIGNALS.patterns, "result = 0 because the field is empty")
        assert score >= 2, f"Expected score >= 2 for zero-because pattern, got {score}"

    def test_mig_llm_rationalization_signal_nlp_clauses_scores_high(self) -> None:
        from hooks.llm_hooks import RATIONALIZATION_SIGNALS

        score = score_signals(RATIONALIZATION_SIGNALS.patterns, "The result is normal and intended.")
        assert score >= 2, f"Expected score >= 2 for NLP rationalization, got {score}"

    def test_mig_llm_rationalization_signal_suppressed_below_threshold(self) -> None:
        from hooks.llm_hooks import RATIONALIZATION_SIGNALS

        score = score_signals(
            RATIONALIZATION_SIGNALS.patterns,
            "result = 0 because the field is empty, investigating root cause from the trace",
        )
        assert score < RATIONALIZATION_SIGNALS.threshold, (
            f"Investigation language should suppress below threshold={RATIONALIZATION_SIGNALS.threshold}, got {score}"
        )

    def test_mig_llm_rationalization_benign_text_below_threshold(self) -> None:
        from hooks.llm_hooks import RATIONALIZATION_SIGNALS

        score = score_signals(RATIONALIZATION_SIGNALS.patterns, "Everything looks fine in the output.")
        assert score < RATIONALIZATION_SIGNALS.threshold

    def test_mig_llm_rationalization_may_be_empty_scores_high(self) -> None:
        from hooks.llm_hooks import RATIONALIZATION_SIGNALS

        score = score_signals(RATIONALIZATION_SIGNALS.patterns, "This field may be empty because the data is missing.")
        assert score >= 2

    def test_mig_llm_rationalization_registered_with_correct_config(self) -> None:
        from hooks.llm_hooks import RATIONALIZATION_SIGNALS

        stop_sub_hooks = [
            h for h in self.app.hooks if h.spec.events == (Event.Stop | Event.SubagentStop) and h.spec.max_fires == 2
        ]
        assert len(stop_sub_hooks) == 2, "rationalization + hydration both on Stop|SubagentStop max_fires=2"
        assert RATIONALIZATION_SIGNALS.window == 15
        assert RATIONALIZATION_SIGNALS.threshold == 2


# ==================== VAL-MIG-LLM-002: Rationalization detector prompt ====================


class TestMigLlmRationalizationPrompt:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.llm_hooks")

    def test_mig_llm_rationalization_prompt_requests_block_verdict_and_uses_builder(self) -> None:
        from hooks.llm_hooks import RATIONALIZATION_PROMPT

        lower = RATIONALIZATION_PROMPT.lower()
        assert "block=true" in lower or "block=false" in lower, (
            "Rationalization prompt must ask for block=true/false verdict"
        )
        assert "rationaliz" in lower, "Prompt should mention rationalization"
        assert "{context}" not in RATIONALIZATION_PROMPT, "Prompt should use Prompt builder, not raw f-string {context}"
        try:
            _ = RATIONALIZATION_PROMPT.format()
        except KeyError as e:
            pytest.fail(f"Prompt has unresolved placeholder: {e}")


# ==================== VAL-MIG-LLM-003: Excuse detector fires on Stop ====================


class TestMigLlmExcuseDetectorEvent:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.llm_hooks")

    def test_mig_llm_excuse_registered_on_stop_with_max_1(self) -> None:
        excuse = _find_gate_by_events_and_max_fires(self.app.hooks, Event.Stop, 1)
        assert excuse.spec.events == Event.Stop
        assert excuse.spec.max_fires == 1

    def test_mig_llm_excuse_nlp_clause_detects_billing_service(self) -> None:
        from hooks.llm_hooks import EXCUSE_SIGNALS

        score = score_signals(EXCUSE_SIGNALS.patterns, "The billing service is external and blocking our work.")
        assert score >= EXCUSE_SIGNALS.threshold, (
            f"Expected score >= {EXCUSE_SIGNALS.threshold} for billing excuse, got {score}"
        )

    def test_mig_llm_excuse_nlp_clause_detects_quota_exceeded(self) -> None:
        from hooks.llm_hooks import EXCUSE_SIGNALS

        score = score_signals(EXCUSE_SIGNALS.patterns, "The quota has been exceeded, we hit the rate limit.")
        assert score >= EXCUSE_SIGNALS.threshold, (
            f"Expected score >= {EXCUSE_SIGNALS.threshold} for quota excuse, got {score}"
        )

    def test_mig_llm_excuse_nlp_clause_detects_service_outage(self) -> None:
        from hooks.llm_hooks import EXCUSE_SIGNALS

        score = score_signals(EXCUSE_SIGNALS.patterns, "The API service outage is preventing us from continuing.")
        assert score >= EXCUSE_SIGNALS.threshold

    def test_mig_llm_excuse_non_excuse_below_threshold(self) -> None:
        from hooks.llm_hooks import EXCUSE_SIGNALS

        score = score_signals(EXCUSE_SIGNALS.patterns, "I fixed the bug in the authentication module.")
        assert score < EXCUSE_SIGNALS.threshold


# ==================== VAL-MIG-LLM-004: Excuse detector prompt ====================


class TestMigLlmExcusePrompt:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.llm_hooks")

    def test_mig_llm_excuse_prompt_mentions_external_service_and_uses_builder(self) -> None:
        from hooks.llm_hooks import EXCUSE_PROMPT

        lower = EXCUSE_PROMPT.lower()
        assert "external" in lower or "service" in lower
        assert "verdict" in lower or "block" in lower or "respond" in lower
        assert "{context}" not in EXCUSE_PROMPT, "Prompt should use Prompt builder, not raw f-string {context}"


# ==================== VAL-MIG-LLM-005: Observe don't infer signals and conditions ====================


class TestMigLlmObserveDontInfer:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.llm_hooks")

    def test_mig_llm_observe_skip_conditions(self) -> None:
        observe = _find_nudge(self.app.hooks)
        skip_if = observe.spec.skip_if

        ran_cmds = [c for c in skip_if if isinstance(c, RanCommand)]
        assert any("uv run lf" in c.pattern for c in ran_cmds), "Should skip_if RanCommand('uv run lf')"

        read_files = [c for c in skip_if if isinstance(c, ReadFile)]
        assert any("DEBUGGING.md" in c.patterns for c in read_files), "Should skip_if ReadFile('DEBUGGING.md')"

        skills = [c for c in skip_if if isinstance(c, UsedSkill)]
        assert any("logfire-trace" in c.name for c in skills), "Should skip_if UsedSkill('logfire-trace')"

    def test_mig_llm_observe_fires_max_2(self) -> None:
        observe = _find_nudge(self.app.hooks)
        assert observe.spec.max_fires == 2

    def test_mig_llm_observe_signal_speculation_scores_above_threshold(self) -> None:
        from hooks.llm_hooks import OBSERVE_SIGNALS

        score = score_signals(
            OBSERVE_SIGNALS.patterns,
            "The issue might be that the function probably doesn't inherit the environment variables.",
        )
        assert score >= OBSERVE_SIGNALS.threshold, (
            f"Speculation should score >= {OBSERVE_SIGNALS.threshold}, got {score}"
        )

    def test_mig_llm_observe_signal_suppressed_by_trace_evidence(self) -> None:
        from hooks.llm_hooks import OBSERVE_SIGNALS

        score = score_signals(
            OBSERVE_SIGNALS.patterns,
            "The issue might be something but the trace shows the connection was correct.",
        )
        assert score < OBSERVE_SIGNALS.threshold, f"Trace evidence should suppress below threshold, got {score}"

    def test_mig_llm_observe_signal_add_note_suppresses(self) -> None:
        from hooks.llm_hooks import OBSERVE_SIGNALS

        score = score_signals(
            OBSERVE_SIGNALS.patterns,
            "The issue might be that but let me use add_note to check.",
        )
        assert score < OBSERVE_SIGNALS.threshold

    def test_mig_llm_observe_has_inline_tests_with_warn_and_allow(self) -> None:
        from captain_hook.testing import Allow, Warn

        observe = _find_nudge(self.app.hooks)
        tests = observe.spec.tests
        assert tests is not None
        warns = [v for v in tests.values() if isinstance(v, Warn)]
        allows = [v for v in tests.values() if isinstance(v, Allow)]
        assert len(warns) >= 4, f"Expected at least 4 Warn inline tests, got {len(warns)}"
        assert len(allows) >= 4, f"Expected at least 4 Allow inline tests, got {len(allows)}"


# ==================== VAL-MIG-LLM-006: Observe don't infer prompt ====================


class TestMigLlmObservePrompt:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.llm_hooks")

    def test_mig_llm_observe_prompt_asks_fire_verdict_and_uses_builder(self) -> None:
        from hooks.llm_hooks import OBSERVE_PROMPT

        lower = OBSERVE_PROMPT.lower()
        assert "fire=true" in lower or "fire=false" in lower
        assert "speculat" in lower or "evidence" in lower
        assert "{context}" not in OBSERVE_PROMPT, "Prompt should use Prompt builder, not raw f-string {context}"


# ==================== VAL-MIG-LLM-007: Hydration workaround signals ====================


class TestMigLlmHydrationSignals:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.llm_hooks")

    def test_mig_llm_hydration_fires_on_stop_and_subagent_stop_max_2(self) -> None:
        hydration_hooks = [
            h for h in self.app.hooks if h.spec.events == (Event.Stop | Event.SubagentStop) and h.spec.max_fires == 2
        ]
        assert len(hydration_hooks) == 2, (
            f"Expected 2 hooks on Stop|SubagentStop with max_fires=2 (rationalization + hydration), "
            f"got {len(hydration_hooks)}"
        )

    def test_mig_llm_hydration_lambda_wrap_scores_high(self) -> None:
        from hooks.llm_hooks import HYDRATION_SIGNALS

        score = score_signals(HYDRATION_SIGNALS.patterns, "I'll wrap this in a lambda to avoid eager hydration.")
        assert score >= HYDRATION_SIGNALS.threshold, (
            f"Lambda wrap should score >= {HYDRATION_SIGNALS.threshold}, got {score}"
        )

    def test_mig_llm_hydration_deferred_access_scores_high(self) -> None:
        from hooks.llm_hooks import HYDRATION_SIGNALS

        score = score_signals(HYDRATION_SIGNALS.patterns, "Use deferred attribute access to bypass hydration.")
        assert score >= HYDRATION_SIGNALS.threshold

    def test_mig_llm_hydration_function_from_name_scores_high(self) -> None:
        from hooks.llm_hooks import HYDRATION_SIGNALS

        score = score_signals(HYDRATION_SIGNALS.patterns, "We should use Function.from_name() instead.")
        assert score >= HYDRATION_SIGNALS.threshold

    def test_mig_llm_hydration_lazy_import_scores_high(self) -> None:
        from hooks.llm_hooks import HYDRATION_SIGNALS

        score = score_signals(
            HYDRATION_SIGNALS.patterns,
            "Let's use lazy import to avoid triggering the hydration error.",
        )
        assert score >= HYDRATION_SIGNALS.threshold

    def test_mig_llm_hydration_deps_fix_below_threshold(self) -> None:
        from hooks.llm_hooks import HYDRATION_SIGNALS

        score = score_signals(HYDRATION_SIGNALS.patterns, "I need to add adding this to deps. deps = [MyClass]")
        assert score < HYDRATION_SIGNALS.threshold, (
            f"Deps fix should suppress below threshold={HYDRATION_SIGNALS.threshold}, got {score}"
        )

    def test_mig_llm_hydration_bypass_restructure_scores_high(self) -> None:
        from hooks.llm_hooks import HYDRATION_SIGNALS

        score = score_signals(
            HYDRATION_SIGNALS.patterns,
            "I'll bypass hydration by restructuring the code to avoid triggering the import.",
        )
        assert score >= HYDRATION_SIGNALS.threshold


# ==================== VAL-MIG-LLM-008: Hydration workaround prompt ====================


class TestMigLlmHydrationPrompt:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.llm_hooks")

    def test_mig_llm_hydration_prompt_mentions_deps_and_verdict_and_uses_builder(self) -> None:
        from hooks.llm_hooks import HYDRATION_PROMPT

        lower = HYDRATION_PROMPT.lower()
        assert "deps" in lower, "Prompt should mention deps"
        assert "block=true" in lower or "block=false" in lower, "Prompt should ask for block verdict"
        assert "{context}" not in HYDRATION_PROMPT


# ==================== VAL-MIG-LLM-009: LLM hook parameters consistent ====================


class TestMigLlmParamsConsistent:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.llm_hooks")

    def test_mig_llm_exactly_four_hooks_with_correct_event_distribution(self) -> None:
        assert len(self.app.hooks) == 4, (
            f"Expected exactly 4 LLM hooks, got {len(self.app.hooks)}: {[h.name for h in self.app.hooks]}"
        )
        stop_subagent_hooks = [h for h in self.app.hooks if h.spec.events == (Event.Stop | Event.SubagentStop)]
        stop_only_hooks = [h for h in self.app.hooks if h.spec.events == Event.Stop]
        post_tool_hooks = [h for h in self.app.hooks if h.spec.events == Event.PostToolUse]
        assert len(stop_subagent_hooks) == 2, "rationalization + hydration on Stop|SubagentStop"
        assert len(stop_only_hooks) == 1, "excuse on Stop only"
        assert len(post_tool_hooks) == 1, "observe on PostToolUse"


# ==================== VAL-MIG-LLM-010: Prompt builder (behavioral) ====================


class TestMigLlmPromptBuilderBehavioral:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.llm_hooks")

    def test_mig_llm_prompts_render_without_fstring_placeholders(self) -> None:
        from hooks import llm_hooks

        for attr in ("RATIONALIZATION_PROMPT", "EXCUSE_PROMPT", "OBSERVE_PROMPT", "HYDRATION_PROMPT"):
            prompt = getattr(llm_hooks, attr)
            try:
                _ = prompt.format()
            except KeyError as e:
                pytest.fail(f"{attr} has unresolved f-string placeholder: {e}")


# ==================== Inline test runner ====================


class TestMigLlmInlineTests:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.llm_hooks")

    def test_mig_llm_observe_inline_tests_all_pass(self) -> None:
        from captain_hook.testing.helpers import run_inline_tests

        results = run_inline_tests(self.app)
        assert len(results) > 0, "Expected inline tests for observe_dont_infer"
        failures = [r for r in results if not r[2]]
        assert not failures, f"Inline test failures: {failures}"
