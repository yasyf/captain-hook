"""Generate or merge the four artifacts a session-attached pack ships against ``pack lint``.

``scaffold_pack`` is non-destructive by construction and refuses before mutating anything: it plans
every artifact first — parsing each existing file, refusing an unparseable one or a capt-hook entry
it can't classify — and only then executes the writes, so a refusal leaves the tree untouched. A
plan whose desired structure already matches the parsed file writes nothing, keeping a conforming
file byte-for-byte identical. The one sanctioned destructive edit is stripping the legacy mirrored
``capt-hook run`` entries the sole-dispatcher contract forbids. It imports nothing from ``cli`` — the
shared identity and predicates live in ``packs.contract``, which ``cli`` re-imports too.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import click

from captain_hook.loader import CONF_MODULE, is_skip_marked
from captain_hook.packs import manager
from captain_hook.packs.contract import (
    DIST_NAME,
    MARKETPLACE_NAME,
    VERSION_FLOOR_RE,
    attach_command,
    command_entries,
    is_attach_argv,
    is_canonical_run_argv,
    search_upward,
)
from captain_hook.review.repo import repo_key

# TODO(release-prep): sync to the tag that ships `pack scaffold` (self-bootstrap must be live on PyPI).
MIN_SCAFFOLD_FLOOR = "9.14.0"

type Verb = Literal["created", "updated", "unchanged"]


@dataclass(frozen=True, slots=True)
class ScaffoldAction:
    """One artifact's outcome: its ``path``, whether it was ``created``/``updated``/``unchanged``, and why."""

    path: Path
    verb: Verb
    detail: str


@dataclass(frozen=True, slots=True)
class Plan:
    path: Path
    verb: Verb
    detail: str
    content: str | None  # None means the file already conforms — no write, so it stays byte-identical.


def scaffold_pack(root: Path, *, name: str, description: str) -> list[ScaffoldAction]:
    """Generate or merge every pack artifact under ``root``, returning one action per artifact.

    ``root`` is the plugin root — the directory the SessionStart attach line targets. Raises
    ``click.ClickException`` (naming the file) when an existing artifact is unparseable or carries a
    capt-hook entry that can't be safely rewritten; every artifact is planned before any write, so a
    refusal leaves the tree untouched.
    """
    nested, pack_root, manifest_path = pack_layout(root)
    manifest_dir = manifest_path.parent
    manifest_plan, manifest = plan_manifest(manifest_path, name=name, description=description)
    plans = [
        manifest_plan,
        plan_hooks_json(root, manifest_dir, nested=nested),
        plan_plugin_json(root, manifest_dir, name=name, description=description),
        plan_marketplace_json(root, manifest_dir, name=name, description=description),
        *plan_starter_hook(manifest.hooks_dir(pack_root)),
    ]
    return [execute(plan) for plan in plans]


def execute(plan: Plan) -> ScaffoldAction:
    if plan.content is not None:
        manager.atomic_write(plan.path, plan.content)
    return ScaffoldAction(plan.path, plan.verb, plan.detail)


def pack_layout(root: Path) -> tuple[bool, Path, Path]:
    """Resolve ``root``'s ``(nested, pack_root, manifest_path)`` the same way ``pack lint`` does."""
    nested = not manager.manifest_in(root).is_file() and manager.manifest_in(root / "hooks").is_file()
    pack_root = root / "hooks" if nested else root
    return nested, pack_root, manager.manifest_in(pack_root)


def dependency_floor() -> str:
    """The scaffolded captain-hook version floor: the installed ``X.Y.0``, clamped up to ``MIN_SCAFFOLD_FLOOR``."""
    installed = (*(int(part) for part in version(DIST_NAME).split(".")[:2]), 0)
    floor = max(installed, tuple(int(part) for part in MIN_SCAFFOLD_FLOOR.split(".")))
    return f">={floor[0]}.{floor[1]}.{floor[2]}"


def resolve_name(root: Path, explicit: str | None) -> str:
    """Resolve the pack slug: an explicit flag (rejected if it renames an existing manifest), else the
    existing manifest name, the plugin.json name, or the sanitized directory basename."""
    existing = manifest_name(root)
    if explicit is not None:
        if existing is not None and existing != explicit:
            raise click.ClickException(
                f"--name {explicit!r} conflicts with the existing {manager.PACK_MANIFEST} name {existing!r}; "
                "drop --name or edit the manifest"
            )
        chosen = explicit
    else:
        chosen = existing or plugin_name(root) or sanitize_slug(root.name)
    if not manager.PACK_NAME_RE.fullmatch(chosen):
        raise click.ClickException(
            f"pack name {chosen!r} must match {manager.PACK_NAME_RE.pattern}; pass --name with a valid slug"
        )
    return chosen


def resolve_description(root: Path, explicit: str | None, name: str) -> str:
    """Resolve the pack description: an explicit flag (rejected if it rewrites an existing manifest's), else
    the existing manifest description, else a generated default."""
    existing = manifest_description(root)
    if explicit is not None:
        if existing is not None and existing != explicit:
            raise click.ClickException(
                f"--description conflicts with the existing {manager.PACK_MANIFEST} description; "
                "drop --description or edit the manifest"
            )
        return explicit
    return existing or f"Captain Hook guards for {name}"


def install_snippet(root: Path, name: str) -> tuple[str, str]:
    """The two README install lines: ``/plugin marketplace add`` + ``/plugin install``.

    Owner/repo come from the git ``origin`` when it is a github.com remote, else placeholders; the
    marketplace defaults to the resolved ``marketplace.json`` name.
    """
    owner_repo = github_owner_repo(root)
    owner, repo = owner_repo or ("<owner>", "<repo>")
    marketplace = marketplace_name(root) or (repo if owner_repo else "<marketplace>")
    return f"/plugin marketplace add {owner}/{repo}", f"/plugin install {name}@{marketplace}"


# --- per-artifact planners -----------------------------------------------------------


def plan_manifest(manifest_path: Path, *, name: str, description: str) -> tuple[Plan, manager.PackManifest]:
    if manifest_path.is_file():
        try:
            manifest = manager.PackManifest.load(manifest_path)
        except (manager.PackError, tomllib.TOMLDecodeError, KeyError, OSError) as e:
            raise click.ClickException(refuse(manifest_path, f"not a valid {manager.PACK_MANIFEST} ({e})")) from e
        return Plan(manifest_path, "unchanged", f"{manifest.name} v{manifest.version}", None), manifest
    manifest = manager.PackManifest(name=name, description=description, hooks="hooks", version="0.1.0")
    return Plan(manifest_path, "created", f"pack manifest for {name}", manifest_template(name, description)), manifest


def plan_hooks_json(root: Path, manifest_dir: Path, *, nested: bool) -> Plan:
    candidates = list(dict.fromkeys([root / "hooks" / "hooks.json", manifest_dir / "hooks.json"]))
    if len(present := [p for p in candidates if p.is_file()]) > 1:
        raise click.ClickException(
            f"ambiguous hooks.json: {present[0]} and {present[1]} both exist — Claude Code loads "
            "<plugin>/hooks/hooks.json; keep exactly one and re-run"
        )
    canonical = attach_command(nested=nested)
    if not present:
        return Plan(candidates[0], "created", "attach-only hooks.json", render_json(attach_only(canonical)))
    data = parse_json(present[0])
    refuse_unrecognized(present[0], data)
    if (desired := merge_hooks_json(data, canonical)) == data:
        return Plan(present[0], "unchanged", "one canonical SessionStart attach", None)
    return Plan(present[0], "updated", "migrated to the canonical SessionStart attach", render_json(desired))


def plan_plugin_json(root: Path, manifest_dir: Path, *, name: str, description: str) -> Plan:
    if (path := search_upward(manifest_dir, ".claude-plugin/plugin.json", "plugin.json")) is None:
        path = root / ".claude-plugin" / "plugin.json"
        return Plan(
            path,
            "created",
            "plugin.json with the captain-hook dependency",
            render_json(new_plugin_json(name, description)),
        )
    data = parse_json(path)
    if (desired := merge_plugin_json(data)) == data:
        return Plan(path, "unchanged", "captain-hook dependency already conforms", None)
    return Plan(path, "updated", "repaired the captain-hook dependency", render_json(desired))


def plan_marketplace_json(root: Path, manifest_dir: Path, *, name: str, description: str) -> Plan:
    if (path := search_upward(manifest_dir, ".claude-plugin/marketplace.json")) is None:
        path = root / ".claude-plugin" / "marketplace.json"
        content = render_json(new_marketplace_json(root, name, description))
        return Plan(path, "created", "marketplace.json allowing the captain-hook dependency", content)
    data = parse_json(path)
    allowed = data.get("allowCrossMarketplaceDependenciesOn")
    allowlist = allowed if isinstance(allowed, list) else []
    if MARKETPLACE_NAME in allowlist:
        return Plan(path, "unchanged", "captain-hook already allowlisted", None)
    desired = data | {"allowCrossMarketplaceDependenciesOn": [*allowlist, MARKETPLACE_NAME]}
    return Plan(path, "updated", "allowlisted the captain-hook cross-marketplace dependency", render_json(desired))


def plan_starter_hook(hooks_dir: Path) -> list[Plan]:
    # A pack with zero loadable hooks fails lint; seed a starter only when the hooks dir is empty.
    if has_hook_files(hooks_dir):
        return []
    return [
        Plan(hooks_dir / "guard.py", "created", "starter block_command guard with inline tests", starter_hook_source())
    ]


# --- merge helpers -------------------------------------------------------------------


def classify_command(cmd: str) -> str:
    """One of ``capt-hook`` (a canonical attach/run entry), ``unrecognized`` (a capt-hook mention we
    won't touch), or ``foreign`` (no capt-hook mention) — the shared lint tokenization."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return "unrecognized" if DIST_NAME in cmd else "foreign"
    if is_attach_argv(argv) or is_canonical_run_argv(argv):
        return "capt-hook"
    return "unrecognized" if DIST_NAME in cmd else "foreign"


def refuse_unrecognized(path: Path, data: dict[str, Any]) -> None:
    if bad := [f"{event}:{cmd!r}" for event, cmd in command_entries(data) if classify_command(cmd) == "unrecognized"]:
        raise click.ClickException(
            f"{path} carries capt-hook entries scaffold can't safely rewrite: {', '.join(bad)}. "
            "Run `capt-hook pack lint` and fix them by hand before scaffolding."
        )


def merge_hooks_json(data: dict[str, Any], canonical: str) -> dict[str, Any]:
    """Strip every canonical capt-hook attach/run entry, prune emptied groups, and append the single
    canonical SessionStart attach, preserving all non-capt-hook entries verbatim."""
    events: dict[str, Any] = {}
    for event, groups in data.get("hooks", {}).items():
        kept_groups = [
            {k: v for k, v in group.items() if k != "hooks"} | {"hooks": kept}
            for group in groups
            if (kept := [entry for entry in group.get("hooks", []) if not strip_capt_hook(entry)])
        ]
        if kept_groups:
            events[event] = kept_groups
    events.setdefault("SessionStart", []).append(attach_group(canonical))
    return data | {"hooks": events}


def strip_capt_hook(entry: dict[str, Any]) -> bool:
    return entry.get("type") == "command" and classify_command(entry["command"]) == "capt-hook"


def merge_plugin_json(data: dict[str, Any]) -> dict[str, Any]:
    deps = data.get("dependencies") or []
    if (idx := next((i for i, d in enumerate(deps) if references_captain_hook(d)), None)) is None:
        return data | {"dependencies": [*deps, captain_dep()]}
    if (merged := merge_captain_dep(deps[idx])) == deps[idx]:
        return data
    return data | {"dependencies": [*deps[:idx], merged, *deps[idx + 1 :]]}


def references_captain_hook(dep: str | dict[str, Any]) -> bool:
    return dep == MARKETPLACE_NAME or (
        isinstance(dep, dict) and MARKETPLACE_NAME in (dep.get("name"), dep.get("marketplace"))
    )


def merge_captain_dep(existing: str | dict[str, Any]) -> dict[str, Any]:
    """The captain-hook dependency object gaining only its missing or invalid fields; a conforming
    version floor is preserved, never bumped."""
    obj = dict(existing) if isinstance(existing, dict) else {}
    if obj.get("name") != MARKETPLACE_NAME:
        obj["name"] = MARKETPLACE_NAME
    if obj.get("marketplace") != MARKETPLACE_NAME:
        obj["marketplace"] = MARKETPLACE_NAME
    if not (isinstance(v := obj.get("version"), str) and VERSION_FLOOR_RE.match(v.strip())):
        obj["version"] = dependency_floor()
    return obj


# --- content builders ----------------------------------------------------------------


def manifest_template(name: str, description: str) -> str:
    return f'name = {toml_str(name)}\nversion = "0.1.0"\ndescription = {toml_str(description)}\nhooks = "hooks"\n'


def attach_only(canonical: str) -> dict[str, Any]:
    return {"hooks": {"SessionStart": [attach_group(canonical)]}}


def attach_group(canonical: str) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": canonical}]}


def captain_dep() -> dict[str, str]:
    return {"name": MARKETPLACE_NAME, "marketplace": MARKETPLACE_NAME, "version": dependency_floor()}


def new_plugin_json(name: str, description: str) -> dict[str, Any]:
    return {"name": name, "description": description, "version": "0.1.0", "dependencies": [captain_dep()]}


def new_marketplace_json(root: Path, name: str, description: str) -> dict[str, Any]:
    owner_repo = github_owner_repo(root)
    return {
        "name": owner_repo[1] if owner_repo else name,
        "owner": {"name": owner_repo[0] if owner_repo else "your-name"},
        "description": description,
        "plugins": [{"name": name, "source": ".", "description": description}],
        "allowCrossMarketplaceDependenciesOn": [MARKETPLACE_NAME],
    }


def starter_hook_source() -> str:
    import importlib.resources

    return (importlib.resources.files("captain_hook") / "templates" / "pack_hook.py.tmpl").read_text()


# --- small readers -------------------------------------------------------------------


def parse_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise click.ClickException(refuse(path, f"not valid JSON ({e})")) from e


def refuse(path: Path, why: str) -> str:
    return f"{path} is present but {why}; fix it by hand — scaffold never overwrites an unparseable file"


def render_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


def toml_str(value: str) -> str:
    return json.dumps(value)


def has_hook_files(hooks_dir: Path) -> bool:
    return hooks_dir.is_dir() and any(
        p.suffix == ".py" and not p.stem.startswith("_") and p.stem != CONF_MODULE and not is_skip_marked(p)
        for p in hooks_dir.iterdir()
    )


def manifest_name(root: Path) -> str | None:
    if not (path := pack_layout(root)[2]).is_file():
        return None
    try:
        return manager.PackManifest.load(path).name
    except (manager.PackError, tomllib.TOMLDecodeError, KeyError, OSError):
        return None


def manifest_description(root: Path) -> str | None:
    if not (path := pack_layout(root)[2]).is_file():
        return None
    try:
        return manager.PackManifest.load(path).description
    except (manager.PackError, tomllib.TOMLDecodeError, KeyError, OSError):
        return None


def plugin_name(root: Path) -> str | None:
    if (path := search_upward(pack_layout(root)[2].parent, ".claude-plugin/plugin.json", "plugin.json")) is None:
        return None
    try:
        name = json.loads(path.read_text()).get("name")
    except (json.JSONDecodeError, OSError):
        return None
    return name if isinstance(name, str) and manager.PACK_NAME_RE.fullmatch(name) else None


def marketplace_name(root: Path) -> str | None:
    if (path := search_upward(pack_layout(root)[2].parent, ".claude-plugin/marketplace.json")) is None:
        return None
    try:
        name = json.loads(path.read_text()).get("name")
    except (json.JSONDecodeError, OSError):
        return None
    return name if isinstance(name, str) and name else None


def github_owner_repo(root: Path) -> tuple[str, str] | None:
    if (
        (key := repo_key(root))
        and key.startswith("github.com/")
        and len(parts := key.removeprefix("github.com/").split("/")) == 2
    ):
        return parts[0], parts[1]
    return None


def sanitize_slug(raw: str) -> str:
    return re.sub(r"^[^a-z]+", "", re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-"))
