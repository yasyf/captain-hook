"""Hook discovery: imports a hooks package, loads its ``conf`` module, and registers every hook module."""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook.app import LoadError, _state, on
from captain_hook.state import PACK_PACKAGE_PREFIX
from captain_hook.types import Event

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

CONF_MODULE = "conf"


def is_test_module(fqn: str) -> bool:
    parts = fqn.split(".")
    return parts[-1].startswith("test_") or parts[-1] == "conftest" or "tests" in parts


def is_skip_marked(path: Path) -> bool:
    """True when the module declares ``__capt_hook_skip__ = True`` — a library, not an auto-loaded hook."""
    return path.is_file() and bool(re.search(r"^__capt_hook_skip__\s*=\s*True\b", path.read_text(), re.MULTILINE))


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


def discover_hooks(hooks_dir: str | Path) -> None:
    from captain_hook.settings import build_settings

    hooks_path = Path(hooks_dir).resolve()
    if str(hooks_path.parent) not in sys.path:
        sys.path.insert(0, str(hooks_path.parent))

    pkg = hooks_path.name
    fresh_this_pass: set[str] = set()

    top_level = {info.name for info in pkgutil.iter_modules([str(hooks_path)]) if not info.name.startswith("_")}

    if CONF_MODULE in top_level:
        conf_module = import_or_reload(f"{pkg}.{CONF_MODULE}", fresh_this_pass)
        _state.settings = build_settings(conf_module)
        if classifier := getattr(conf_module, "classifier", None):
            _state.classifier = classifier

    all_modules = {
        info.name
        for info in pkgutil.walk_packages([str(hooks_path)], prefix=f"{pkg}.")
        if not info.name.rpartition(".")[2].startswith("_")
        and not is_test_module(info.name)
        and not is_skip_marked(hooks_path.parent / Path(*info.name.split(".")).with_suffix(".py"))
    }

    for fqn in sorted(all_modules - {f"{pkg}.{CONF_MODULE}"}):
        # Broad catch is deliberate (see discover_pack): a single bad module must
        # not abort discovery. Logged loudly at WARNING and recorded in
        # ``state.load_errors`` so ``capt-hook test`` fails on it, never silently swallowed.
        try:
            import_or_reload(fqn, fresh_this_pass)
        except Exception as exc:
            logger.bind(module=fqn).opt(exception=True).warning("skipped unloadable hook module")
            _state.load_errors.append(LoadError(fqn, exc))


def ensure_pack_package(fqn: str, search_paths: list[str]) -> ModuleType:
    if existing := sys.modules.get(fqn):
        return existing
    spec = importlib.util.spec_from_loader(fqn, loader=None, is_package=True)
    if spec is None:
        raise ImportError(f"cannot synthesize package spec for {fqn}")
    package = importlib.util.module_from_spec(spec)
    package.__path__ = search_paths
    package.__package__ = fqn
    sys.modules[fqn] = package
    return package


def import_pack_module(fqn: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(fqn, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fqn] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(fqn, None)
        raise
    return module


def register_nlp_provisioning() -> None:
    """Register the shared SessionStart hook that eagerly provisions NLP resources.

    Called once per discovery pass when any resolved pack manifest declares ``nlp``,
    so both event dispatch and settings generation see the async hook.
    """

    @on(Event.SessionStart, async_=True)
    def provision_nlp_resources(evt: BaseHookEvent) -> None:
        from captain_hook.util.model_cache import ensure_nlp_resources

        ensure_nlp_resources()


def discover_pack(name: str, pack_dir: Path) -> None:
    pkg = f"{PACK_PACKAGE_PREFIX}.{re.sub(r'\W', '_', name)}"
    ensure_pack_package(PACK_PACKAGE_PREFIX, [])
    ensure_pack_package(pkg, [str(pack_dir)])
    for path in sorted(pack_dir.glob("*.py")):
        if path.stem.startswith("_") or path.stem == CONF_MODULE or is_test_module(path.stem):
            continue
        if is_skip_marked(path):
            continue
        # Broad catch is deliberate: one unloadable or non-hook .py must not abort
        # the whole pack. Logged loudly at WARNING and recorded in ``load_errors`` so
        # ``capt-hook test`` fails on it, never silently swallowed.
        try:
            import_pack_module(f"{pkg}.{path.stem}", path)
        except Exception as exc:
            logger.bind(file=str(path)).opt(exception=True).warning("skipped unloadable hook file")
            _state.load_errors.append(LoadError(str(path), exc, pack=name))
