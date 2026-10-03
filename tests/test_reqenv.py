from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

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
            "HOOKS_PROFILE",
            "CLAUDE_PROJECT_DIR",
            "FACTORY_A",
            "XDG_CACHE_HOME",
            "CEREBRAS_API_KEY",
            "ORCA_TERMINAL_HANDLE",
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
        with reqenv.use_request(overrides({"CAPT_HOOK_CLIENT_TIMEOUT": "1s"})):
            assert reqenv.getenv("CLAUDE_PROJECT_DIR") is None
            assert reqenv.getenv("CLAUDE_PROJECT_DIR", "fallback") == "fallback"

    def test_bound_non_whitelisted_key_passes_through_to_process_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/bin:/usr/bin")
        with reqenv.use_request(overrides({"PATH": "/should-be-ignored"})):
            assert reqenv.getenv("PATH") == "/bin:/usr/bin"

    def test_default_survives_typed_non_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        assert reqenv.getenv("CLAUDE_PROJECT_DIR", 10.0) == 10.0


class TestProvider:
    @pytest.mark.parametrize("value", ["claude", "codex"])
    def test_reads_explicit_request_provider(self, value: str) -> None:
        with reqenv.use_request(overrides({"CAPT_HOOK_PROVIDER": value})):
            assert reqenv.provider() == value

    def test_omitted_provider_remains_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAPT_HOOK_PROVIDER", "codex")
        with reqenv.use_request(overrides({})):
            assert reqenv.provider() == "claude"

    @pytest.mark.parametrize("value", ["", "unknown", "Codex"])
    def test_unknown_provider_is_rejected(self, value: str) -> None:
        with reqenv.use_request(overrides({"CAPT_HOOK_PROVIDER": value})):
            with pytest.raises(ValueError, match="unsupported hook provider"):
                reqenv.provider()


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


class TestCheckpoint:
    def test_is_a_no_op_outside_a_fan_out(self) -> None:
        reqenv.checkpoint()

    def test_passes_until_the_flag_is_set_then_raises(self) -> None:
        flag = reqenv.Cutoff()
        with reqenv.abandonable(flag):
            reqenv.checkpoint()
            flag.set()
            with pytest.raises(reqenv.Abandoned):
                reqenv.checkpoint()
        reqenv.checkpoint()

    def test_an_abandoned_hook_stops_walking_a_tree(self, tmp_path: Path) -> None:
        from captain_hook.util.globbing import walked_paths
        from captain_hook.util.vcs import scanned_names

        (tmp_path / "nested").mkdir()
        flag = reqenv.Cutoff()
        flag.set()
        with reqenv.abandonable(flag):
            with pytest.raises(reqenv.Abandoned):
                list(walked_paths(tmp_path))
            with pytest.raises(reqenv.Abandoned):
                list(scanned_names(tmp_path))
        assert [path.name for path in walked_paths(tmp_path)] == ["nested"]

    def test_abandoned_escapes_a_handlers_broad_except(self) -> None:
        assert not issubclass(reqenv.Abandoned, Exception)

    def test_publish_runs_until_the_cutoff_closes_then_refuses(self) -> None:
        cutoff = reqenv.Cutoff()
        published: list[str] = []
        with reqenv.abandonable(cutoff):
            assert reqenv.publish(lambda: published.append("verdict")) is None
            cutoff.close()
            with pytest.raises(reqenv.Abandoned):
                reqenv.publish(lambda: published.append("late"))
        assert published == ["verdict"]
        assert reqenv.publish(lambda: "unbound") == "unbound"

    def test_close_refuses_a_publisher_it_stopped_waiting_for(self) -> None:
        cutoff = reqenv.Cutoff()
        entered, release = threading.Event(), threading.Event()

        def hold() -> str:
            entered.set()
            assert release.wait(timeout=5.0)
            return "held"

        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(cutoff.publish, hold)
            assert entered.wait(timeout=5.0)
            assert cutoff.close(timeout=0.0) is False
            assert cutoff.is_set()
            release.set()
            with pytest.raises(reqenv.Abandoned):
                holder.result(timeout=5.0)
        with reqenv.abandonable(cutoff), pytest.raises(reqenv.Abandoned):
            reqenv.publish(lambda: "late")
        assert reqenv.Cutoff().close(timeout=0.0) is True

    def test_a_publisher_the_closure_waited_for_is_counted(self) -> None:
        cutoff = reqenv.Cutoff()
        entered, release = threading.Event(), threading.Event()

        def hold() -> str:
            entered.set()
            assert release.wait(timeout=5.0)
            return "held"

        with ThreadPoolExecutor(max_workers=2) as pool:
            holder = pool.submit(cutoff.publish, hold)
            assert entered.wait(timeout=5.0)
            closer = pool.submit(cutoff.close)
            release.set()
            assert holder.result(timeout=5.0) == "held"
            assert closer.result(timeout=5.0) is True

    def test_a_settlement_crossing_the_cutoff_is_refused_and_fails_a_later_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [1_000.0]
        monkeypatch.setattr(reqenv, "time", SimpleNamespace(time=lambda: now[0]))
        cutoff = reqenv.Cutoff(deadline_unix_ms=1_000_500)
        settled: list[str] = []

        def settle_past_the_cutoff() -> str:
            settled.append("late")
            now[0] += 1.0
            return "late"

        assert cutoff.publish(lambda: "timely") == "timely"
        with pytest.raises(reqenv.Abandoned):
            cutoff.publish(settle_past_the_cutoff)
        assert settled == ["late"]
        assert cutoff.close(timeout=0.0) is False


class TestAbandoned:
    def test_unbound_is_a_scratch_list(self) -> None:
        reqenv.abandoned().append("hook")
        assert reqenv.abandoned() == []

    def test_bound_collects_on_the_request(self) -> None:
        with reqenv.use_request(overrides({})) as bound:
            reqenv.abandoned().append("hook")
        assert bound.abandoned == ["hook"]
