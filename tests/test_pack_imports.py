from __future__ import annotations

import sys
from pathlib import Path

import pytest

from captain_hook import app
from captain_hook.loader import discover_pack

RELATIVE_IMPORT_SRC = (
    "from ._common import SHARED\nfrom captain_hook import Event, hook\nhook(Event.PreToolUse, message=str(SHARED))\n"
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
