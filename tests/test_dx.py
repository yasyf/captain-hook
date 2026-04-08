from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import captain_hook
from captain_hook import __all__ as all_exports
from captain_hook.app import (
    _state,
    hook as register_hook,
    on,
    register,
    reset,
)
from captain_hook.types import (
    Command,
    Event,
    FilePath,
    Tool,
)

SRC_ROOT = Path(__file__).parent.parent / "src" / "captain_hook"

@pytest.fixture(autouse=True)
def _clean_state():
    reset()
    yield
    reset()



class TestDocstrings:
    def test_all_exports_have_docstrings(self) -> None:
        missing = [
            name
            for name in all_exports
            if not getattr(getattr(captain_hook, name, None), "__doc__", None)
        ]
        assert not missing, f"Symbols in __all__ missing docstrings: {missing}"

    def test_no_signature_shaped_docstrings(self) -> None:
        sig_shaped = []
        for name in all_exports:
            obj = getattr(captain_hook, name, None)
            if obj is None:
                continue
            doc = getattr(obj, "__doc__", None)
            if not doc:
                continue
            first_line = doc.strip().split("\n")[0]
            if re.match(r"^" + re.escape(name) + r"\(", first_line):
                sig_shaped.append(name)
        assert not sig_shaped, (
            f"Symbols with auto-generated signature-shaped docstrings: {sig_shaped}"
        )


class TestPyTyped:
    def test_py_typed_marker_exists(self) -> None:
        assert (SRC_ROOT / "py.typed").exists(), "py.typed marker file is missing"


class TestAllExportsImportable:
    def test_all_symbols_importable(self) -> None:
        import captain_hook

        missing = []
        for name in all_exports:
            if not hasattr(captain_hook, name):
                missing.append(name)
        assert not missing, f"Symbols in __all__ but not importable: {missing}"

    def test_star_import_no_error(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "from captain_hook import *"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Star import failed: {result.stderr}"


class TestHookWithoutMessage:
    def test_hook_method_requires_message(self) -> None:
        with pytest.raises(TypeError, match="message="):
            register_hook(Event.PreToolUse, block=True)

    def test_hook_method_without_any_args_requires_message(self) -> None:
        with pytest.raises(TypeError, match="message="):
            register_hook(Event.PreToolUse)

    def test_register_declarative_without_message_warns(self) -> None:
        with pytest.raises(TypeError, match="message="):
            register(Event.PreToolUse, block=True)


class TestInvalidConditionType:
    def test_non_tcondition_in_only_if_raises(self) -> None:
        with pytest.raises(TypeError, match="only_if"):
            register_hook(
                Event.PreToolUse,
                only_if=["Bash"],  # type: ignore[list-item]
                message="test",
            )

    def test_non_tcondition_in_skip_if_raises(self) -> None:
        with pytest.raises(TypeError, match="skip_if"):
            register_hook(
                Event.PreToolUse,
                skip_if=[42],  # type: ignore[list-item]
                message="test",
            )

    def test_valid_conditions_pass(self) -> None:
        register_hook(
            Event.PreToolUse,
            only_if=[Tool("Bash"), FilePath("*.py"), Command(r"rm")],
            message="valid",
        )
        assert len(_state.hooks) == 1

    def test_error_lists_valid_types(self) -> None:
        with pytest.raises(TypeError, match="Tool|FilePath|Command"):
            register_hook(
                Event.PreToolUse,
                only_if=["invalid"],  # type: ignore[list-item]
                message="test",
            )


class TestInvalidEventNameInCLI:
    def test_invalid_event_lists_valid_names(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "captain_hook", "run", "InvalidEvent"],
            capture_output=True,
            text=True,
            input="{}",
        )
        assert result.returncode != 0
        assert "PreToolUse" in result.stderr
        assert "PostToolUse" in result.stderr
        assert "Stop" in result.stderr


class TestWrongHandlerSignature:
    def test_no_params_raises(self) -> None:
        with pytest.raises(TypeError, match="signature"):

            @on(Event.PreToolUse)
            def bad_handler():  # type: ignore[empty-body]
                pass

    def test_too_many_params_raises(self) -> None:
        with pytest.raises(TypeError, match="signature"):

            @on(Event.PreToolUse)
            def bad_handler(evt, extra, more):  # type: ignore[empty-body]
                pass

    def test_required_keyword_only_raises(self) -> None:
        with pytest.raises(TypeError, match="required keyword-only"):

            @on(Event.PreToolUse)
            def bad_handler(evt, *, mode: str):  # type: ignore[empty-body]
                pass

    def test_required_keyword_only_via_register_raises(self) -> None:

        def bad_handler(evt, *, mode: str):  # type: ignore[empty-body]
            pass

        deco = register(Event.PreToolUse)
        assert deco is not None
        with pytest.raises(TypeError, match="required keyword-only"):
            deco(bad_handler)

    def test_optional_keyword_only_passes(self) -> None:

        @on(Event.PreToolUse)
        def handler_with_default(evt, *, mode: str = "default"):  # type: ignore[empty-body]
            pass

        assert len(_state.hooks) == 1

    def test_valid_signature_passes(self) -> None:

        @on(Event.PreToolUse)
        def good_handler(evt):  # type: ignore[empty-body]
            pass

        assert len(_state.hooks) == 1


class TestPrimitiveOutsideContext:
    def test_nudge_registers_on_singleton(self) -> None:
        from captain_hook.app import hook, on
        from captain_hook.primitives.nudge import nudge

        before = len(_state.hooks)
        nudge("test message")
        assert len(_state.hooks) == before + 1
        _state.hooks.pop()

    def test_block_command_registers_on_singleton(self) -> None:
        from captain_hook.app import hook, on
        from captain_hook.primitives.commands import block_command

        before = len(_state.hooks)
        block_command(["git", "stash"], reason="test")
        assert len(_state.hooks) == before + 1
        _state.hooks.pop()

    def test_lint_registers_on_singleton(self) -> None:
        from captain_hook.app import hook, on
        from captain_hook.primitives.lint import lint

        def check(content: str) -> list[str]:
            return []

        before = len(_state.hooks)
        lint(check, message="test {violations}")
        assert len(_state.hooks) == before + 1
        _state.hooks.pop()

    def test_state_is_singleton(self) -> None:
        from captain_hook.app import _state as s1
        from captain_hook.app import _state as s2

        assert s1 is s2


class TestCLIHelp:
    def test_help_shows_subcommands(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "captain_hook", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "run" in result.stdout
        assert "generate-settings" in result.stdout
        assert "test" in result.stdout

    def test_run_help_shows_events(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "captain_hook", "run", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "PreToolUse" in result.stdout
