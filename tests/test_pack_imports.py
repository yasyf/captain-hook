from __future__ import annotations

import sys
from pathlib import Path

import pytest

from captain_hook import app
from captain_hook.loader import discover_pack

RELATIVE_IMPORT_SRC = (
    "from ._common import SHARED\nfrom captain_hook import Event, hook\nhook(Event.PreToolUse, message=str(SHARED))\n"
)
ON_HANDLER_SRC = "from captain_hook import Event, on\n\n\n@on(Event.PostToolUse)\ndef check(evt):\n    return None\n"


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
