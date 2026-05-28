from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path
from types import ModuleType

from pydantic_settings import BaseSettings

from captain_hook.app import State, _state

CONF_MODULE = "conf"


def build_hook_settings(module: ModuleType) -> BaseSettings | ModuleType:
    if importlib.util.find_spec("captain_hook.settings"):
        settings_mod = importlib.import_module("captain_hook.settings")
        return settings_mod.build_settings(module)
    return module


def import_or_reload(fqn: str, fresh_this_pass: set[str]) -> ModuleType:
    if fqn in fresh_this_pass:
        return sys.modules[fqn]
    before = set(sys.modules)
    if fqn in sys.modules:
        mod = importlib.reload(sys.modules[fqn])
    else:
        mod = importlib.import_module(fqn)
    fresh_this_pass.update(set(sys.modules) - before)
    fresh_this_pass.add(fqn)
    return mod


def discover_hooks(hooks_dir: str | Path, state: State | None = None) -> None:
    target = state or _state
    hooks_path = Path(hooks_dir).resolve()
    if str(hooks_path.parent) not in sys.path:
        sys.path.insert(0, str(hooks_path.parent))

    pkg = hooks_path.name
    fresh_this_pass: set[str] = set()

    top_level = {info.name for info in pkgutil.iter_modules([str(hooks_path)]) if not info.name.startswith("_")}

    if CONF_MODULE in top_level:
        conf_module = import_or_reload(f"{pkg}.{CONF_MODULE}", fresh_this_pass)
        target.settings = build_hook_settings(conf_module)
        if classifier := getattr(conf_module, "classifier", None):
            target.classifier = classifier

    all_modules = {
        info.name
        for info in pkgutil.walk_packages([str(hooks_path)], prefix=f"{pkg}.")
        if not info.name.rpartition(".")[2].startswith("_")
    }

    for fqn in sorted(all_modules - {f"{pkg}.{CONF_MODULE}"}):
        import_or_reload(fqn, fresh_this_pass)
