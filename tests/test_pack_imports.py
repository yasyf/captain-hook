from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import captain_hook
from captain_hook import app
from captain_hook.loader import discover_pack

RELATIVE_IMPORT_SRC = (
    "from ._common import SHARED\nfrom captain_hook import Event, hook\nhook(Event.PreToolUse, message=str(SHARED))\n"
)
ON_HANDLER_SRC = "from captain_hook import Event, on\n\n\n@on(Event.PostToolUse)\ndef check(evt):\n    return None\n"
PRIMITIVES_SRC = (
    "from captain_hook import gate, llm_nudge, nudge, rewrite_command, set_tool_input\n"
    "nudge('Remember the release notes')\n"
    "gate('Run the suite before stopping')\n"
    "llm_nudge('Is the agent guessing?', message='Observe first')\n"
    "set_tool_input('model', 'sonnet', tool='Agent')\n"
    "rewrite_command('cat $$$ARGS', 'bat $$$ARGS')\n"
)
INSTALLED_PROBE = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "import captain_hook\n"
    "from captain_hook.app import _state\n"
    "from captain_hook.loader import discover_pack\n"
    "discover_pack('ccx', Path(sys.argv[1]))\n"
    "print(json.dumps({'package': captain_hook.__file__,"
    " 'hooks': [[h.state_key, h._state_identity, h.source_file] for h in _state.hooks]}))\n"
)


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    p = tmp_path / "pack"
    p.mkdir()
    (p / "_common.py").write_text("SHARED = 1\n")
    (p / "alpha.py").write_text(RELATIVE_IMPORT_SRC)
    return p


def test_pack_relative_import_resolves(pack: Path, isolate_modules: None) -> None:
    discover_pack("ccx-rel", pack)

    alpha = sys.modules["captain_hook._packs.ccx_rel.alpha"]
    assert alpha.SHARED == 1
    assert len(app._state.hooks) == 1
    assert app._state.hooks[0].spec.message == "1"


def test_pack_parent_packages_registered_with_path(pack: Path, isolate_modules: None) -> None:
    discover_pack("ccx-parent", pack)

    assert "captain_hook._packs" in sys.modules
    leaf = sys.modules["captain_hook._packs.ccx_parent"]
    assert leaf.__path__ == [str(pack)]
    assert leaf.__package__ == "captain_hook._packs.ccx_parent"


def test_pack_underscore_sibling_importable_on_demand(tmp_path: Path, isolate_modules: None) -> None:
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    (standalone / "_common.py").write_text("SHARED = 1\n")
    (standalone / "alpha.py").write_text("from captain_hook import Event, hook\nhook(Event.PreToolUse, message='m')\n")

    discover_pack("ccx-sibling", standalone)

    # The auto-load loop skips _-prefixed files and nothing else imports it, so it
    # is absent from sys.modules until an explicit on-demand import pulls it in.
    assert "captain_hook._packs.ccx_sibling._common" not in sys.modules
    common = __import__("captain_hook._packs.ccx_sibling._common", fromlist=["SHARED"])
    assert common.SHARED == 1


def test_ensure_pack_package_idempotent(pack: Path, isolate_modules: None) -> None:
    discover_pack("ccx-idem", pack)
    first = sys.modules["captain_hook._packs.ccx_idem"]

    (pack / "beta.py").write_text(RELATIVE_IMPORT_SRC)
    discover_pack("ccx-idem", pack)

    assert sys.modules["captain_hook._packs.ccx_idem"] is first  # not clobbered
    assert sys.modules["captain_hook._packs.ccx_idem.beta"].SHARED == 1


def _pack_root(base: Path, src: str = ON_HANDLER_SRC) -> Path:
    base.mkdir(parents=True)
    (base / "guard.py").write_text(src)
    return base


def test_state_key_stable_across_pack_root_moves(tmp_path: Path, isolate_modules: None) -> None:
    # A plugin update relands a pack under a fresh versioned cache dir. The state key must key on
    # pack identity (name + pack-root-relative path), not the absolute source, so a re-attach after
    # the update does not reset every max_fires counter mid-session.
    root_a = _pack_root(tmp_path / "v1" / "hooks")
    root_b = _pack_root(tmp_path / "v2" / "hooks")

    app.reset()
    discover_pack("ccx", root_a)
    key_a = app._state.hooks[0].state_key

    app.reset()
    discover_pack("ccx", root_b)
    key_b = app._state.hooks[0].state_key

    assert key_a == key_b


def installed_state_keys(install: Path, pack: Path) -> list[list[str]]:
    install.mkdir()
    (install / "captain_hook").symlink_to(Path(captain_hook.__file__).parent, target_is_directory=True)
    probe = subprocess.run(
        [sys.executable, "-c", INSTALLED_PROBE, str(pack)],
        cwd=install,
        env={**os.environ, "PYTHONPATH": str(install)},
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = json.loads(probe.stdout)
    assert loaded["package"] == str(install / "captain_hook" / "__init__.py")
    return loaded["hooks"]


def test_primitive_state_keys_stable_across_capt_hook_installs(tmp_path: Path) -> None:
    pack = _pack_root(tmp_path / "pack", PRIMITIVES_SRC)

    first = installed_state_keys(tmp_path / "v1", pack)
    second = installed_state_keys(tmp_path / "v2", pack)

    assert len(first) == 5
    assert first == second
    assert {(identity, source) for _, identity, source in first} == {("ccx\0guard.py", str(pack / "guard.py"))}


def test_framework_hooks_key_on_their_package_relative_path(tmp_path: Path, isolate_modules: None) -> None:
    from captain_hook.cli import CliState

    (hooks := tmp_path / ".claude" / "hooks").mkdir(parents=True)
    CliState(root=tmp_path, hooks=str(hooks)).discover(scope="all")

    framework = {"announce_pr_status", "announce_faults"}
    assert {h._state_identity for h in app._state.hooks if h.name in framework} == {"loader.py"}


def test_shared_decorator_keeps_each_handler_file(tmp_path: Path, isolate_modules: None) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "_shared.py").write_text(
        "from captain_hook import Event, on\n\npost = on(Event.PostToolUse, max_fires=1)\n"
    )
    for stem in ("alpha", "beta"):
        (pack / f"{stem}.py").write_text("from ._shared import post\n\n\n@post\ndef check(evt):\n    return None\n")

    app.reset()
    discover_pack("ccx", pack)

    assert sorted(h._state_identity for h in app._state.hooks) == ["ccx\0alpha.py", "ccx\0beta.py"]


def test_state_key_differs_for_two_packs_same_hook_name(tmp_path: Path, isolate_modules: None) -> None:
    # Two packs each register an @on handler named "check"; the pack name in the identity keeps
    # their state keys (and thus max_fires counters) distinct.
    app.reset()
    discover_pack("alpha", _pack_root(tmp_path / "alpha"))
    discover_pack("beta", _pack_root(tmp_path / "beta"))

    keys = {h.state_key for h in app._state.hooks}
    assert len(app._state.hooks) == 2
    assert len(keys) == 2


def test_pack_module_rolls_back_partial_registration(tmp_path: Path, isolate_modules: None) -> None:
    # A module that registers a valid sync hook then raises (async_=True on a decision event)
    # contributes nothing: the good hook is rolled back and the failure recorded.
    from captain_hook.app import AsyncDecisionError

    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "guard.py").write_text(
        "from captain_hook import Event, hook\n\n"
        'hook(Event.PostToolUse, message="good")\n'
        'hook(Event.Stop, message="bad", async_=True)\n'
    )

    app.reset()
    discover_pack("ccx", pack)

    assert app._state.hooks == []
    assert len(app._state.load_errors) == 1
    assert isinstance(app._state.load_errors[0].exc, AsyncDecisionError)
