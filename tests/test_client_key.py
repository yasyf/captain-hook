from __future__ import annotations

import pytest

from capt_hook_client import key
from captain_hook.util.paths import resolve_cache_dir


def test_protocol_is_one() -> None:
    assert key.PROTOCOL == 1


class TestRequestEnv:
    def test_whitelisted_prefixes_and_xdg_kept(self) -> None:
        env = {
            "CAPT_HOOK_A": "1",
            "CAPTAIN_HOOK_B": "2",
            "HOOKS_C": "3",
            "CLAUDE_D": "4",
            "FACTORY_E": "5",
            "XDG_CACHE_HOME": "/c",
        }
        assert key.request_env(env) == env

    def test_unlisted_keys_dropped(self) -> None:
        env = {"PATH": "/usr/bin", "HOME": "/home/x", "XDG_RUNTIME_DIR": "/run", "CAPT_HOOK_KEEP": "y"}
        assert key.request_env(env) == {"CAPT_HOOK_KEEP": "y"}


class TestWorkerKey:
    def test_hex_shape(self) -> None:
        digest = key.worker_key("/proj", {})
        assert len(digest) == 16 and all(c in "0123456789abcdef" for c in digest)

    def test_unlisted_env_var_does_not_change_key(self) -> None:
        base = {"CAPTAIN_HOOK_STATE_DIR": "/s"}
        assert key.worker_key("/proj", base) == key.worker_key("/proj", base | {"CLAUDE_CODE_SESSION_ID": "abc"})

    def test_claude_config_dir_excluded(self) -> None:
        base = {"HOOKS_X": "1"}
        assert key.worker_key("/proj", base) == key.worker_key("/proj", base | {"CLAUDE_CONFIG_DIR": "/pool/acct-05"})

    @pytest.mark.parametrize(
        "changed",
        [
            {"CAPTAIN_HOOK_STATE_DIR": "/other"},
            {"CAPT_HOOK_DECISIONS_DB": "/db"},
            {"HOOKS_EXTRA": "z"},
            {"XDG_CACHE_HOME": "/cache2"},
        ],
    )
    def test_listed_env_var_changes_key(self, changed: dict[str, str]) -> None:
        base = {"CAPTAIN_HOOK_STATE_DIR": "/s", "HOOKS_EXTRA": "a"}
        assert key.worker_key("/proj", base) != key.worker_key("/proj", base | changed)

    def test_realpath_collapses_symlinked_root(self, tmp_path) -> None:
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        assert key.worker_key(str(link), {}) == key.worker_key(str(target), {})


class TestRunDir:
    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAPT_HOOK_RUN_DIR", "/custom/run")
        assert str(key.run_dir()) == "/custom/run"

    def test_identity_with_resolve_cache_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CAPT_HOOK_RUN_DIR", raising=False)
        assert key.run_dir() == resolve_cache_dir() / "run"


class TestPaths:
    def test_all_paths_under_run_dir_with_suffixes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAPT_HOOK_RUN_DIR", "/r")
        assert key.socket_path("abcd") == key.run_dir() / "abcd.sock"
        assert key.lock_path("abcd") == key.run_dir() / "abcd.lock"
        assert key.meta_path("abcd") == key.run_dir() / "abcd.json"
        assert key.log_path("abcd") == key.run_dir() / "abcd.log"


class TestBuildFingerprint:
    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAPT_HOOK_CLIENT_BUILD", "pinned-42")
        assert key.build_fingerprint() == "pinned-42"

    def test_mtime_ns_size_of_client_py(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CAPT_HOOK_CLIENT_BUILD", raising=False)
        stat = key.CLIENT_PATH.stat()
        assert key.build_fingerprint() == f"{stat.st_mtime_ns}-{stat.st_size}"
