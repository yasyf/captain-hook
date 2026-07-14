from __future__ import annotations

import os
from pathlib import Path

import pytest

from captain_hook.util import reqenv


def overrides(env: dict[str, str], *, cwd: str = "/work", session_id: str = "sess") -> reqenv.RequestOverrides:
    return reqenv.RequestOverrides(env=env, cwd=cwd, client_ppid=17, session_id=session_id)


class TestWhitelist:
    @pytest.mark.parametrize(
        "key",
        [
            "CAPT_HOOK_X",
            "CAPTAIN_HOOK_STATE_DIR",
            "HOOKS_DAEMON_IDLE_S",
            "CLAUDE_PROJECT_DIR",
            "FACTORY_A",
            "XDG_CACHE_HOME",
        ],
    )
    def test_prefixed_and_exact_keys_are_whitelisted(self, key: str) -> None:
        assert reqenv.is_whitelisted(key)

    @pytest.mark.parametrize("key", ["PATH", "HOME", "XDG_DATA_HOME", "PWD", "SHELL"])
    def test_unrelated_keys_are_not_whitelisted(self, key: str) -> None:
        assert not reqenv.is_whitelisted(key)


class TestGetenv:
    def test_unbound_reads_process_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/cold")
        assert reqenv.current() is None
        assert reqenv.getenv("CLAUDE_PROJECT_DIR") == "/cold"

    def test_bound_whitelisted_key_resolves_from_request_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/cold")
        with reqenv.use_request(overrides({"CLAUDE_PROJECT_DIR": "/warm"})):
            assert reqenv.getenv("CLAUDE_PROJECT_DIR") == "/warm"

    def test_bound_whitelisted_absent_is_authoritatively_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A daemon-inherited whitelisted var must never leak into a request that omits it.
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/daemon-inherited")
        with reqenv.use_request(overrides({"CAPT_HOOK_RUN_DIR": "/run"})):
            assert reqenv.getenv("CLAUDE_PROJECT_DIR") is None
            assert reqenv.getenv("CLAUDE_PROJECT_DIR", "fallback") == "fallback"

    def test_bound_non_whitelisted_key_passes_through_to_process_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/bin:/usr/bin")
        with reqenv.use_request(overrides({"PATH": "/should-be-ignored"})):
            assert reqenv.getenv("PATH") == "/bin:/usr/bin"

    def test_default_survives_typed_non_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CAPT_HOOK_CLIENT_BUILD", raising=False)
        assert reqenv.getenv("CAPT_HOOK_CLIENT_BUILD", 10.0) == 10.0


class TestEnvMap:
    def test_unbound_is_the_process_environ(self) -> None:
        assert reqenv.env_map() is os.environ

    def test_bound_overlays_request_env_on_process_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/bin")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/cold")
        with reqenv.use_request(overrides({"CLAUDE_PROJECT_DIR": "/warm"})):
            env = reqenv.env_map()
            assert env["PATH"] == "/bin"
            assert env["CLAUDE_PROJECT_DIR"] == "/warm"

    def test_bound_strips_whitelisted_key_absent_from_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The B1 probe: a worker started with CLAUDE_CONFIG_DIR=/account-a must not leak it into a
        # request that omitted it — call_cli children would otherwise inherit account-a's config.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/account-a")
        monkeypatch.setenv("PATH", "/bin")
        with reqenv.use_request(overrides({"CLAUDE_PROJECT_DIR": "/warm"})):
            env = reqenv.env_map()
            assert "CLAUDE_CONFIG_DIR" not in env  # whitelisted + absent from request → stripped, not inherited
            assert env["CLAUDE_PROJECT_DIR"] == "/warm"  # request-provided whitelisted key survives
            assert env["PATH"] == "/bin"  # non-whitelisted daemon env still inherited verbatim


class TestCwd:
    def test_unbound_is_process_cwd(self) -> None:
        assert reqenv.cwd() == Path.cwd()

    def test_bound_uses_request_cwd(self) -> None:
        with reqenv.use_request(overrides({}, cwd="/req/dir")):
            assert reqenv.cwd() == Path("/req/dir")


class TestUseRequest:
    def test_binding_is_scoped_and_resets_on_exit(self) -> None:
        assert reqenv.current() is None
        with reqenv.use_request(overrides({})) as bound:
            assert reqenv.current() is bound
        assert reqenv.current() is None
