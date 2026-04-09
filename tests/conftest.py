from __future__ import annotations

import sys

import pytest

from captain_hook.app import reset


@pytest.fixture(autouse=True)
def clean_state():
    reset()
    yield
    reset()


@pytest.fixture
def isolate_modules():
    snapshot_modules = set(sys.modules.keys())
    snapshot_path = sys.path[:]
    yield
    for key in set(sys.modules.keys()) - snapshot_modules:
        del sys.modules[key]
    sys.path[:] = snapshot_path
