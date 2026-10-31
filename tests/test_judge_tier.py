from types import SimpleNamespace
from unittest.mock import patch

import pytest
from spawnllm import ClaudeCliBackend, CodexCliBackend

from captain_hook.context import run_spec


def planned(backend, env=None):
    specs = []

    def run_sync(spec, *, backend):
        specs.append(spec)
        return SimpleNamespace(error=None, result=SimpleNamespace(raw="ok"))

    with patch("spawnllm.run_sync", run_sync):
        run_spec("prompt", backend, "small", None, agent=False, cwd=None, timeout=5, attempts=1, tools=None, env=env)
    return specs[0]


@pytest.mark.parametrize("env", [None, {"CODEX_API_KEY": "key"}], ids=["local", "api-actor"])
def test_codex_judges_run_on_the_standard_tier(env):
    config = planned(CodexCliBackend(), env).provider_configs["codex"]
    assert config.service_tier is None
    assert config.bypass_approvals_and_sandbox is (env is not None)
    assert "service_tier" not in " ".join(CodexCliBackend().invocation(planned(CodexCliBackend(), env)).argv)


def test_claude_judges_carry_no_codex_config():
    assert "codex" not in planned(ClaudeCliBackend()).provider_configs
