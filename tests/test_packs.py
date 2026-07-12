from __future__ import annotations

import sys
import tarfile
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from loguru import logger

import captain_hook
from captain_hook import app
from captain_hook.cli import cli
from captain_hook.dispatch import dispatch
from captain_hook.loader import discover_pack, import_pack_module
from captain_hook.packs import manager
from captain_hook.testing.helpers import input_to_event
from captain_hook.testing.types import Input
from captain_hook.types import Event
from captain_hook.util import http

PACKS_DIR = Path(captain_hook.__file__).parent / "packs"
EXPECTED_BUILTINS = {"general", "python", "go", "steering", "fixes"}
GENERAL_HOOKS = {
    "commands",
    "comments",
    "detours",
    "docs",
    "models",
    "plans",
    "prompts",
    "review",
    "tasks",
    "tombstones",
}
PYTHON_HOOKS = {"style", "testing", "toolchain"}
GO_HOOKS = {"testing", "toolchain"}
# lib.py carries __capt_hook_skip__ so it is a non-underscore file the loader skips; the
# layout test counts .py files, so it appears here, but only steering.py registers hooks.
STEERING_HOOKS = {"steering", "teammates"}
FIXES_HOOKS = {"teammate_permissions"}
HOOK_SRC = "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='m')\n"
SRC_USES_FILE = (
    "from pathlib import Path\n"
    "from captain_hook import Event, hook\n"
    "_here = Path(__file__).parent\n"
    "hook(Event.PreToolUse, message=str(_here))\n"
)
WARNING_NO = logger.level("WARNING").no


def write_pack(root: Path, name: str, *, hooks: str = ".", version: str = "0.1.0", manifest_subdir: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest_dir = root / manifest_subdir if manifest_subdir else root
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / manager.PACK_MANIFEST).write_text(
        f'name = "{name}"\nversion = "{version}"\ndescription = "d"\nhooks = "{hooks}"\n'
    )
    return root


def make_pack_tarball(dest: Path, *, name: str, top: str) -> Path:
    src = dest / "src"
    write_pack(src, name, hooks="hooks")
    (src / "hooks").mkdir()
    (src / "hooks" / "h.py").write_text(HOOK_SRC)
    tarball = dest / f"{name}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(src, arcname=top)
    return tarball


class Clock:
    """A movable monotonic-enough clock; patch onto manager.time.time to drive the TTL."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeGitHub:
    """In-memory GitHub: serves resolve_ref/resolve_commit JSON and the tarball, counting every network call.

    ``default_branch`` -> ref for a bare source; ``latest_tag`` -> tag for @latest;
    ``sha`` -> the commit every ref resolves to. ``calls`` proves the within-TTL hot
    path makes no requests.
    """

    name: str
    sha: str
    tarball: Path
    default_branch: str = "main"
    latest_tag: str = "v9.9.9"
    json_calls: int = 0
    download_calls: int = 0
    seen_refs: list[str] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return self.json_calls + self.download_calls

    def get_json(self, url: str) -> Any:
        self.json_calls += 1
        if url.endswith("/releases/latest"):
            return {"tag_name": self.latest_tag}
        if "/commits/" in url:
            self.seen_refs.append(url.rsplit("/commits/", 1)[1])
            return {"sha": self.sha}
        return {"default_branch": self.default_branch}

    def download(self, url: str, dest: Path) -> None:
        self.download_calls += 1
        dest.write_bytes(self.tarball.read_bytes())

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
        monkeypatch.setattr(http, "github_get_json", self.get_json)
        monkeypatch.setattr(http, "github_download", self.download)
        return self


def fake_github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str, sha: str, **kw: Any) -> FakeGitHub:
    monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
    tarball = make_pack_tarball(tmp_path / f"src-{sha[:8]}", name=name, top=f"{name}-{sha[:8]}")
    return FakeGitHub(name=name, sha=sha, tarball=tarball, **kw).install(monkeypatch)


def go_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise http.GitHubFetchError("offline")

    monkeypatch.setattr(http, "github_get_json", boom)
    monkeypatch.setattr(http, "github_download", boom)


def install_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tarball: Path, sha: str) -> None:
    monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(manager, "resolve_commit", lambda source: sha)
    monkeypatch.setattr(http, "github_download", lambda url, dest: dest.write_bytes(tarball.read_bytes()))


# --- builtin pack content -------------------------------------------------------------


def test_expected_builtin_packs_present() -> None:
    on_disk = {p.name for p in PACKS_DIR.iterdir() if p.is_dir() and (p / manager.PACK_MANIFEST).is_file()}
    assert on_disk == EXPECTED_BUILTINS
    assert set(manager.builtin_packs()) == EXPECTED_BUILTINS


@pytest.mark.parametrize(
    ("name", "hook_stems"),
    [
        ("general", GENERAL_HOOKS),
        ("python", PYTHON_HOOKS),
        ("go", GO_HOOKS),
        ("steering", STEERING_HOOKS),
        ("fixes", FIXES_HOOKS),
    ],
    ids=["general", "python", "go", "steering", "fixes"],
)
def test_builtin_pack_layout(name: str, hook_stems: set[str]) -> None:
    pack_dir = manager.builtin_packs()[name]
    manifest = manager.PackManifest.load(pack_dir / manager.PACK_MANIFEST)
    assert manifest.name == name
    assert {p.stem for p in pack_dir.glob("*.py") if not p.stem.startswith("_")} == hook_stems


@pytest.mark.parametrize(
    "name",
    [
        "fragments/deliverable_rubric.md",
        "fragments/workflow_script_header.md",
        "models/prose_spawn_gate.md",
        "models/prose_workflow_nudge.md",
        "models/review_routing_spawn_nudge.md",
        "models/review_routing_workflow_nudge.md",
        "models/writing_docs_spawn_nudge.md",
        "models/writing_docs_workflow_nudge.md",
        "models/implementation_spawn_nudge.md",
        "models/inline_edit_nudge.md",
    ],
)
def test_general_pack_prompts_are_packaged(name: str) -> None:
    """The general pack's Prompt.load .md files must ship as package data (wheel/plugin)."""
    assert (resources.files(captain_hook) / "packs/general/prompts" / name).is_file()


def test_fixes_pack_scopes_to_native_bash(isolate_modules: None, tmp_path: Path) -> None:
    discover_pack("fixes", PACKS_DIR / "fixes")

    def decision(tool: str, command: str) -> dict[str, Any] | None:
        evt = input_to_event(
            Event.PermissionRequest,
            Input(tool=tool, tool_input={"command": command}, agent_id="tm1", skip_permissions=True),
        )
        return dispatch(Event.PermissionRequest, evt, session_dir=tmp_path)

    allowed = decision("Bash", "echo hi")
    assert allowed is not None
    assert allowed["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert decision("mcp__srv__Bash", "echo hi") is None
    assert decision("mcp__ops__Bash", "rm -rf /") is None


# --- PackSource ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "owner", "repo", "ref"),
    [
        ("github:acme/guards", "acme", "guards", None),
        ("github:acme/guards@v1.2", "acme", "guards", "v1.2"),
        ("github:a-b/c.d@feat/x", "a-b", "c.d", "feat/x"),
    ],
)
def test_pack_source_round_trip(raw: str, owner: str, repo: str, ref: str | None) -> None:
    source = manager.PackSource.parse(raw)
    assert (source.owner, source.repo, source.ref) == (owner, repo, ref)
    assert str(source) == raw


@pytest.mark.parametrize("raw", ["acme/guards", "github:acme", "gitlab:a/b", "github:a/b@"])
def test_pack_source_rejects_invalid(raw: str) -> None:
    with pytest.raises(manager.PackError):
        manager.PackSource.parse(raw)


# --- PackManifest --------------------------------------------------------------------


def test_manifest_load_and_hooks_dir(tmp_path: Path) -> None:
    pack = write_pack(tmp_path / "p", "acme-guards", hooks="hooks")
    manifest = manager.PackManifest.load(pack / manager.PACK_MANIFEST)
    assert (manifest.name, manifest.version, manifest.hooks) == ("acme-guards", "0.1.0", "hooks")
    assert manifest.hooks_dir(pack) == pack / "hooks"


@pytest.mark.parametrize(
    "manifest_text",
    [
        pytest.param('name = "Bad_Name"\nversion = "0"\ndescription = "d"\nhooks = "."\n', id="rejects_bad_name"),
        pytest.param(None, id="missing_file"),
    ],
)
def test_manifest_rejects(tmp_path: Path, manifest_text: str | None) -> None:
    if manifest_text is not None:
        (tmp_path / manager.PACK_MANIFEST).write_text(manifest_text)
    with pytest.raises(manager.PackError):
        manager.PackManifest.load(tmp_path / manager.PACK_MANIFEST)


def test_manifest_missing_field_fails_loud(tmp_path: Path) -> None:
    (tmp_path / manager.PACK_MANIFEST).write_text('name = "x"\nversion = "0"\nhooks = "."\n')
    with pytest.raises(KeyError):
        manager.PackManifest.load(tmp_path / manager.PACK_MANIFEST)


@pytest.mark.parametrize(
    ("nlp_line", "expected"),
    [
        pytest.param("", False, id="absent_defaults_false"),
        pytest.param("nlp = true\n", True, id="true_parses"),
        pytest.param("nlp = false\n", False, id="false_parses"),
    ],
)
def test_manifest_nlp_flag(tmp_path: Path, nlp_line: str, expected: bool) -> None:
    (tmp_path / manager.PACK_MANIFEST).write_text(
        f'name = "x"\nversion = "0"\ndescription = "d"\nhooks = "."\n{nlp_line}'
    )
    assert manager.PackManifest.load(tmp_path / manager.PACK_MANIFEST).nlp is expected


def test_manifest_in_prefers_claude(tmp_path: Path) -> None:
    (tmp_path / manager.PACK_MANIFEST).write_text("root")
    assert manager.manifest_in(tmp_path) == tmp_path / manager.PACK_MANIFEST
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / manager.PACK_MANIFEST).write_text("claude")
    assert manager.manifest_in(tmp_path) == claude / manager.PACK_MANIFEST


def test_manifest_in_missing_returns_root(tmp_path: Path) -> None:
    found = manager.manifest_in(tmp_path)
    assert found == tmp_path / manager.PACK_MANIFEST
    assert not found.is_file()


# --- packs.toml IO -------------------------------------------------------------------


@pytest.fixture
def packs_toml(tmp_path: Path) -> Path:
    return tmp_path / "packs.toml"


def test_packs_toml_round_trip(packs_toml: Path) -> None:
    builtin = manager.BuiltinPack(name="general")
    external = manager.ExternalPack(name="acme", source=manager.PackSource.parse("github:a/b@v1"), commit="a" * 40)
    manager.atomic_write(packs_toml, manager.render_packs_toml([external, builtin]))
    assert manager.read_entries(packs_toml) == [external, builtin]  # rendered sorted by name: "acme" < "general"


def test_upsert_replaces_same_name(packs_toml: Path) -> None:
    manager.upsert_entry(packs_toml, manager.ExternalPack("acme", manager.PackSource.parse("github:a/b@v1"), "a" * 40))
    manager.upsert_entry(packs_toml, manager.ExternalPack("acme", manager.PackSource.parse("github:a/b@v2"), "b" * 40))
    (entry,) = manager.read_entries(packs_toml)
    assert isinstance(entry, manager.ExternalPack) and entry.commit == "b" * 40


def test_delete_entry(packs_toml: Path) -> None:
    manager.upsert_entry(packs_toml, manager.BuiltinPack("general"))
    manager.upsert_entry(packs_toml, manager.BuiltinPack("python"))
    manager.delete_entry(packs_toml, "general")
    assert manager.read_entries(packs_toml) == [manager.BuiltinPack("python")]
    with pytest.raises(manager.PackError):
        manager.delete_entry(packs_toml, "missing")


def test_read_entries_accepts_source_only_as_moving(packs_toml: Path) -> None:
    packs_toml.write_text('[packs.acme]\nsource = "github:a/b@latest"\n')
    (entry,) = manager.read_entries(packs_toml)
    assert entry == manager.ExternalPack("acme", manager.PackSource.parse("github:a/b@latest"), commit=None)


def test_source_only_round_trips_without_commit(packs_toml: Path) -> None:
    moving = manager.ExternalPack("acme", manager.PackSource.parse("github:a/b@latest"), commit=None)
    manager.atomic_write(packs_toml, manager.render_packs_toml([moving]))
    assert "commit" not in packs_toml.read_text()
    assert manager.read_entries(packs_toml) == [moving]


def test_read_entries_rejects_unknown_keys(packs_toml: Path) -> None:
    packs_toml.write_text('[packs.acme]\ncommit = "abc"\n')  # commit without source is not a valid entry
    with pytest.raises(manager.PackError):
        manager.read_entries(packs_toml)


# --- fetch / cache -------------------------------------------------------------------


def test_strip_top_level(tmp_path: Path) -> None:
    tarball = make_pack_tarball(tmp_path, name="x", top="repo-sha")
    with tarfile.open(tarball) as tf:
        names = {m.path for m in manager.strip_top_level(tf)}
    assert {"capt-hook.toml", "hooks/h.py"} <= names
    assert not any(n.startswith("repo-sha") for n in names)


def test_fetch_pack_caches_and_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tarball = make_pack_tarball(tmp_path, name="acme-guards", top="guards-abc")
    sha = "d" * 40
    install_fetch(monkeypatch, tmp_path, tarball, sha)

    resolved = manager.fetch_pack(manager.PackSource.parse("github:acme/guards@v1"))

    assert resolved.entry == manager.ExternalPack("acme-guards", manager.PackSource.parse("github:acme/guards@v1"), sha)
    assert resolved.manifest.name == "acme-guards"
    assert (resolved.path / "h.py").is_file()
    assert manager.find_cached("acme-guards", sha) == tmp_path / "cache" / f"acme-guards@{sha}"
    assert manager.find_cached("acme-guards", "e" * 40) is None


def test_fetch_pack_caches_only_manifest_and_hooks_subdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    write_pack(repo, "acme-guards", hooks="plugin/hooks")
    (repo / "plugin" / "hooks").mkdir(parents=True)
    (repo / "plugin" / "hooks" / "h.py").write_text(HOOK_SRC)
    (repo / "README.md").write_text("# big repo\n")
    (repo / "go.mod").write_text("module example.com/big\n")
    (repo / "src").mkdir()
    (repo / "src" / "big.txt").write_text("x" * 4096)
    tarball = tmp_path / "repo.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(repo, arcname="big-repo-sha")

    sha = "d" * 40
    install_fetch(monkeypatch, tmp_path, tarball, sha)

    resolved = manager.fetch_pack(manager.PackSource.parse("github:acme/guards@v1"))

    cached = tmp_path / "cache" / f"acme-guards@{sha}"
    assert {p.name for p in cached.iterdir()} == {manager.PACK_MANIFEST, "plugin", manager.SHA_MARKER}
    assert (cached / "plugin" / "hooks" / "h.py").is_file()
    assert (resolved.path / "h.py").is_file()
    for junk in ("README.md", "go.mod", "src"):
        assert not (cached / junk).exists(), f"{junk} leaked into the cache"


def test_fetch_pack_claude_manifest_caches_manifest_and_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    write_pack(repo, "acme-guards", hooks="plugin/hooks", manifest_subdir=".claude")
    (repo / ".claude" / "settings.json").write_text("{}\n")  # other .claude/ content must not leak
    (repo / "plugin" / "hooks").mkdir(parents=True)
    (repo / "plugin" / "hooks" / "h.py").write_text(HOOK_SRC)
    tarball = tmp_path / "repo.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(repo, arcname="big-repo-sha")

    sha = "d" * 40
    install_fetch(monkeypatch, tmp_path, tarball, sha)

    resolved = manager.fetch_pack(manager.PackSource.parse("github:acme/guards@v1"))

    cached = tmp_path / "cache" / f"acme-guards@{sha}"
    assert {p.name for p in cached.iterdir()} == {".claude", "plugin", manager.SHA_MARKER}
    assert manager.manifest_in(cached) == cached / ".claude" / manager.PACK_MANIFEST
    assert not (cached / ".claude" / "settings.json").exists(), "other .claude/ content leaked into the cache"
    assert (resolved.path / "h.py").is_file()  # resolved.path == cached/plugin/hooks
    assert resolved.manifest.name == "acme-guards"
    re_resolved = manager.resolve_external(resolved.entry)
    assert re_resolved is not None and re_resolved.manifest.name == "acme-guards"


def test_fetch_pack_prefers_claude_manifest_over_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    write_pack(repo, "root-pack", hooks="hooks")  # root manifest
    write_pack(repo, "claude-pack", hooks="hooks", manifest_subdir=".claude")  # .claude wins
    (repo / "hooks").mkdir()
    (repo / "hooks" / "h.py").write_text(HOOK_SRC)
    tarball = tmp_path / "repo.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(repo, arcname="sha-top")

    sha = "a" * 40
    install_fetch(monkeypatch, tmp_path, tarball, sha)

    resolved = manager.fetch_pack(manager.PackSource.parse("github:acme/guards@v1"))
    assert resolved.manifest.name == "claude-pack"


def test_fetch_pack_root_hooks_caches_full_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    write_pack(repo, "acme-flat", hooks=".")
    (repo / "h.py").write_text(HOOK_SRC)
    tarball = tmp_path / "repo.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(repo, arcname="flat-sha")

    sha = "e" * 40
    install_fetch(monkeypatch, tmp_path, tarball, sha)

    resolved = manager.fetch_pack(manager.PackSource.parse("github:acme/flat@v1"))

    cached = tmp_path / "cache" / f"acme-flat@{sha}"
    assert (cached / manager.PACK_MANIFEST).is_file()
    assert (cached / "h.py").is_file()
    assert (resolved.path / "h.py").is_file()


def test_fetch_pack_missing_manifest_fails_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# no manifest here\n")
    tarball = tmp_path / "repo.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(repo, arcname="no-manifest-sha")

    install_fetch(monkeypatch, tmp_path, tarball, "f" * 40)

    with pytest.raises(manager.PackError):
        manager.fetch_pack(manager.PackSource.parse("github:acme/empty@v1"))


# --- resolution ----------------------------------------------------------------------


def test_resolve_enabled_builtin(tmp_path: Path) -> None:
    manager.upsert_entry(manager.packs_toml_path(tmp_path), manager.BuiltinPack("general"))
    resolved, missing = manager.resolve_enabled_packs(tmp_path)
    assert missing == []
    (general,) = resolved
    assert general.entry == manager.BuiltinPack("general")
    assert general.manifest.name == "general"


def test_resolve_unknown_builtin_fails_loud(tmp_path: Path) -> None:
    manager.upsert_entry(manager.packs_toml_path(tmp_path), manager.BuiltinPack("nope"))
    with pytest.raises(manager.PackError):
        manager.resolve_enabled_packs(tmp_path)


def test_resolve_uncached_external_offline_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
    go_offline(monkeypatch)
    manager.upsert_entry(
        manager.packs_toml_path(tmp_path),
        manager.ExternalPack("acme", manager.PackSource.parse("github:a/b@v1"), "a" * 40),
    )
    resolved, missing = manager.resolve_enabled_packs(tmp_path)
    assert (resolved, missing) == ([], ["acme"])  # uncached pin + offline auto-fetch fails -> the one loud path


# --- loader.discover_pack ------------------------------------------------------------


@pytest.mark.parametrize(
    ("stems", "present", "absent"),
    [
        pytest.param(("hook", "_skip", "conf"), ("hook",), (), id="skips_underscore_and_conf"),
        pytest.param(("hook", "test_hook", "conftest"), ("hook",), ("test_hook", "conftest"), id="skips_test_files"),
    ],
)
def test_discover_pack_skips(
    tmp_path: Path, stems: tuple[str, ...], present: tuple[str, ...], absent: tuple[str, ...]
) -> None:
    pack = tmp_path / "p"
    pack.mkdir()
    for stem in stems:
        (pack / f"{stem}.py").write_text(HOOK_SRC)
    discover_pack("solo", pack)
    assert len(app._state.hooks) == 1
    for module in present:
        assert f"captain_hook._packs.solo.{module}" in sys.modules
    for module in absent:
        assert f"captain_hook._packs.solo.{module}" not in sys.modules


def test_discover_pack_namespaces_avoid_collision(tmp_path: Path) -> None:
    for name in ("a", "b"):
        pack = tmp_path / name
        pack.mkdir()
        (pack / "commands.py").write_text(HOOK_SRC)
        discover_pack(name, pack)
    assert len(app._state.hooks) == 2
    assert "captain_hook._packs.a.commands" in sys.modules
    assert "captain_hook._packs.b.commands" in sys.modules


def test_discover_pack_sanitizes_name(tmp_path: Path) -> None:
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "h.py").write_text(HOOK_SRC)
    discover_pack("acme-guards@d4", pack)
    assert "captain_hook._packs.acme_guards_d4.h" in sys.modules


def test_discover_pack_skips_marked_library_but_keeps_it_importable(tmp_path: Path, isolate_modules: None) -> None:
    pack = tmp_path / "marked"
    pack.mkdir()
    (pack / "hook.py").write_text(HOOK_SRC)
    # A non-underscore library file the loader must skip on the strength of the marker
    # alone (it would otherwise be auto-loaded). It still imports cleanly on demand.
    (pack / "lib.py").write_text("__capt_hook_skip__ = True\nSHARED = 42\n")
    discover_pack("ccx-marked", pack)

    assert len(app._state.hooks) == 1  # only hook.py registered; the marked lib stayed inert
    assert "captain_hook._packs.ccx_marked.hook" in sys.modules
    assert "captain_hook._packs.ccx_marked.lib" not in sys.modules  # the auto-load loop never imported it

    lib = __import__("captain_hook._packs.ccx_marked.lib", fromlist=["SHARED"])  # but it remains importable
    assert lib.SHARED == 42
    assert len(app._state.hooks) == 1  # importing the marked library registers nothing


def test_discover_pack_warns_and_continues_on_unloadable(tmp_path: Path, logcap: Any) -> None:
    pack = tmp_path / "p"
    pack.mkdir()
    (pack / "good.py").write_text(HOOK_SRC)
    (pack / "bad.py").write_text("raise RuntimeError('boom')\n")
    discover_pack("solo", pack)
    assert len(app._state.hooks) == 1
    assert "captain_hook._packs.solo.good" in sys.modules
    assert "captain_hook._packs.solo.bad" not in sys.modules
    assert any("bad.py" in r.message and r.levelno >= WARNING_NO for r in logcap.records)


def test_import_pack_module_sets_file(tmp_path: Path) -> None:
    path = tmp_path / "uses_file.py"
    path.write_text(SRC_USES_FILE)
    module = import_pack_module("captain_hook._packs.solo.uses_file", path)
    assert module.__file__ == str(path)
    assert len(app._state.hooks) == 1


# --- steering pack: enabled registration --------------------------------------------


def test_enabling_steering_pack_registers_hooks() -> None:
    resolved = manager.resolve_builtin("steering")
    discover_pack("steering", resolved.path)
    # Five: steering.py registers the two signal nudges, the band-aid-plan llm_nudge, and the
    # deferral llm_gate; teammates.py registers the teammate-digest SubagentStart nudge.
    assert len(app._state.hooks) == 5
    assert "captain_hook._packs.steering.steering" in sys.modules
    assert "captain_hook._packs.steering.teammates" in sys.modules


def test_steering_deferral_gate_skips_in_plan_mode() -> None:
    from captain_hook.types import InPlanMode, Waiting

    discover_pack("steering", manager.resolve_builtin("steering").path)
    # The lone Stop-gate is the deferral gate; plan mode cannot ship code, so it must skip there
    # (additively with the auto Waiting() guard), leaving plan-content policing to the ExitPlanMode nudge.
    (gate,) = (h for h in app._state.hooks if h.spec.events & (Event.Stop | Event.SubagentStop))
    assert gate.spec.skip_if == (Waiting(), InPlanMode())


# --- CLI -----------------------------------------------------------------------------


def test_cli_pack_add_list_remove(tmp_path: Path) -> None:
    runner = CliRunner()
    add = runner.invoke(cli, ["--root", str(tmp_path), "pack", "add", "general"])
    assert add.exit_code == 0, add.output
    assert "[packs.general]" in manager.packs_toml_path(tmp_path).read_text()

    listed = runner.invoke(cli, ["--root", str(tmp_path), "pack", "list"])
    assert "general" in listed.output and "10 hooks" in listed.output

    remove = runner.invoke(cli, ["--root", str(tmp_path), "pack", "remove", "general"])
    assert remove.exit_code == 0
    assert manager.read_entries(manager.packs_toml_path(tmp_path)) == []


def test_cli_pack_add_rejects_invalid_target(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "pack", "add", "not-a-pack"])
    assert result.exit_code != 0
    assert not manager.packs_toml_path(tmp_path).exists()


def test_cli_pack_list_reports_import_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_dir = write_pack(tmp_path / "badpack", "badpack", hooks=".")
    (pack_dir / "boom.py").write_text("raise RuntimeError('kaboom')\n")
    resolved = [
        manager.ResolvedPack(
            entry=manager.BuiltinPack(name="badpack"),
            path=pack_dir,
            manifest=manager.PackManifest(name="badpack", version="0.1.0", description="d", hooks="."),
        )
    ]
    monkeypatch.setattr(manager, "resolve_enabled_packs", lambda _root: (resolved, []))

    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "pack", "list"])

    assert result.exit_code == 0, result.output
    assert "badpack" in result.output  # the pack still lists
    assert "!  badpack: boom.py failed to import - RuntimeError: kaboom" in result.output


# --- @latest / ref resolution --------------------------------------------------------


@pytest.mark.parametrize(
    ("ref_suffix", "gh_kwargs", "sha", "expected_seen_refs"),
    [
        # @latest resolves the release tag, not the literal "latest", before the commits endpoint.
        pytest.param("@latest", {"latest_tag": "v3.2.1"}, "d" * 40, ["v3.2.1"], id="latest_uses_release_endpoint"),
        # a bare source resolves the repo's default branch.
        pytest.param("", {"default_branch": "trunk"}, "e" * 40, ["trunk"], id="bare_uses_default_branch"),
        # an explicit ref passes through with no release lookup.
        pytest.param("@v1.2", {}, "f" * 40, ["v1.2"], id="explicit_tag_passes_through"),
    ],
)
def test_resolve_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ref_suffix: str,
    gh_kwargs: dict[str, Any],
    sha: str,
    expected_seen_refs: list[str],
) -> None:
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha=sha, **gh_kwargs)
    assert manager.resolve_commit(manager.PackSource.parse(f"github:acme/acme{ref_suffix}")) == sha
    assert gh.seen_refs == expected_seen_refs


# --- auto-fetch on miss --------------------------------------------------------------


def test_cache_miss_pinned_auto_fetches_and_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "a" * 40
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha=sha)
    manager.upsert_entry(
        manager.packs_toml_path(tmp_path),
        manager.ExternalPack("acme", manager.PackSource.parse("github:acme/acme@v1"), sha),
    )
    assert manager.find_cached("acme", sha) is None  # cold cache

    resolved, missing = manager.resolve_enabled_packs(tmp_path)

    assert missing == []
    (pack,) = resolved
    assert pack.manifest.name == "acme" and (pack.path / "h.py").is_file()
    assert manager.find_cached("acme", sha) is not None  # the miss self-healed
    assert gh.download_calls == 1


def test_cache_miss_latest_auto_fetches_via_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "b" * 40
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha=sha, latest_tag="v2.0.0")
    monkeypatch.setattr(manager.time, "time", Clock())
    manager.upsert_entry(
        manager.packs_toml_path(tmp_path),
        manager.ExternalPack("acme", manager.PackSource.parse("github:acme/acme@latest"), commit=None),
    )

    resolved, missing = manager.resolve_enabled_packs(tmp_path)

    assert missing == []
    assert resolved[0].manifest.name == "acme"
    assert gh.seen_refs == ["v2.0.0"] and gh.download_calls == 1
    meta = manager.PackMeta.load(manager.meta_path("acme"))
    assert meta is not None and meta.commit == sha  # resolved commit recorded in the per-machine sidecar


def test_cache_miss_offline_other_packs_still_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
    go_offline(monkeypatch)
    path = manager.packs_toml_path(tmp_path)
    manager.upsert_entry(path, manager.BuiltinPack("general"))
    manager.upsert_entry(path, manager.ExternalPack("acme", manager.PackSource.parse("github:a/b@v1"), "a" * 40))

    resolved, missing = manager.resolve_enabled_packs(tmp_path)

    assert missing == ["acme"]  # one bad pack does not block the others
    assert [r.entry.name for r in resolved] == ["general"]


# --- 24h TTL + content-hash fast path ------------------------------------------------


def enable_moving(tmp_path: Path, *, ref: str = "@latest") -> None:
    manager.upsert_entry(
        manager.packs_toml_path(tmp_path),
        manager.ExternalPack("acme", manager.PackSource.parse(f"github:acme/acme{ref}"), commit=None),
    )


def test_within_ttl_does_no_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(manager.time, "time", clock)
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha="a" * 40)
    enable_moving(tmp_path)

    manager.resolve_enabled_packs(tmp_path)  # warms cache + sidecar
    calls_after_warm = gh.calls
    assert calls_after_warm > 0

    clock.advance(23 * 60 * 60)  # still inside the 24h window
    resolved, missing = manager.resolve_enabled_packs(tmp_path)

    assert missing == [] and resolved[0].manifest.name == "acme"
    assert gh.calls == calls_after_warm  # ZERO new network calls within the TTL window


def test_past_ttl_reresolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(manager.time, "time", clock)
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha="a" * 40)
    enable_moving(tmp_path)

    manager.resolve_enabled_packs(tmp_path)
    calls_after_warm = gh.calls

    clock.advance(25 * 60 * 60)  # past the 24h window
    manager.resolve_enabled_packs(tmp_path)

    assert gh.json_calls > calls_after_warm  # the ref was re-resolved over the network
    meta = manager.PackMeta.load(manager.meta_path("acme"))
    assert meta is not None and meta.checked_at == clock.now  # sidecar timestamp refreshed


def test_past_ttl_moved_ref_fetches_new_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(manager.time, "time", clock)
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha="a" * 40)
    enable_moving(tmp_path)
    manager.resolve_enabled_packs(tmp_path)

    new_tarball = make_pack_tarball(tmp_path / "src-new", name="acme", top="acme-new")
    gh.sha, gh.tarball = "c" * 40, new_tarball  # the moving ref now points at a new commit
    clock.advance(25 * 60 * 60)

    resolved, missing = manager.resolve_enabled_packs(tmp_path)

    assert missing == [] and resolved[0].manifest.name == "acme"
    assert manager.find_cached("acme", "c" * 40) is not None
    meta = manager.PackMeta.load(manager.meta_path("acme"))
    assert meta is not None and meta.commit == "c" * 40


def test_offline_during_ttl_refresh_falls_back_to_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(manager.time, "time", clock)
    fake_github(tmp_path, monkeypatch, name="acme", sha="a" * 40)
    enable_moving(tmp_path)
    manager.resolve_enabled_packs(tmp_path)  # warm

    clock.advance(25 * 60 * 60)  # TTL expired -> a refresh is due
    go_offline(monkeypatch)  # ...but the network is gone
    resolved, missing = manager.resolve_enabled_packs(tmp_path)

    assert missing == []  # offline always works: fall back to the cached commit
    assert resolved[0].manifest.name == "acme"
    meta = manager.PackMeta.load(manager.meta_path("acme"))
    assert meta is not None and meta.checked_at == clock() - 25 * 60 * 60  # not bumped on a failed refresh


def test_fastpath_unchanged_skips_all_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(manager.time, "time", clock)
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha="a" * 40)
    enable_moving(tmp_path)

    manager.resolve_enabled_packs(tmp_path)  # writes the fastpath sidecar
    baseline = gh.calls

    # packs.toml byte-identical, every pack cached within TTL -> pure fast skip.
    for _ in range(3):
        clock.advance(60)  # well within TTL
        manager.resolve_enabled_packs(tmp_path)
    assert gh.calls == baseline  # the hot path never touched the network


def test_fastpath_invalidated_by_packs_toml_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(manager.time, "time", clock)
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha="a" * 40)
    enable_moving(tmp_path)
    manager.resolve_enabled_packs(tmp_path)
    after_warm = gh.calls

    manager.upsert_entry(manager.packs_toml_path(tmp_path), manager.BuiltinPack("general"))  # toml changed
    manager.resolve_enabled_packs(tmp_path)

    # The hash mismatch drops the fast path; the moving pack is still fresh within TTL, so
    # resolution stays local (no network), but the fastpath sidecar is rewritten for the new toml.
    assert gh.calls == after_warm
    assert manager.fastpath_path(tmp_path).read_text() == manager.toml_hash(tmp_path)


# --- pinned lockfile path (existing source+commit) -----------------------------------


def test_pinned_lockfile_resolves_without_ref_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "a" * 40
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha=sha)
    manager.upsert_entry(
        manager.packs_toml_path(tmp_path),
        manager.ExternalPack("acme", manager.PackSource.parse("github:acme/acme@v1"), sha),
    )
    manager.resolve_enabled_packs(tmp_path)  # cold: fetch the pinned commit directly
    assert gh.json_calls == 0  # a hard pin never resolves a ref
    assert gh.download_calls == 1

    monkeypatch.setattr(manager.time, "time", Clock(start=1_700_000_000.0 + 10 * 24 * 60 * 60))
    resolved, missing = manager.resolve_enabled_packs(tmp_path)

    assert missing == [] and resolved[0].manifest.name == "acme"
    assert gh.download_calls == 1  # pinned + cached: no TTL, no re-fetch even years later
    assert manager.PackMeta.load(manager.meta_path("acme")) is None  # pins keep no sidecar


# --- pack add / update / list moving-ref consistency ---------------------------------


def test_cli_pack_add_latest_is_source_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager.time, "time", Clock())
    fake_github(tmp_path, monkeypatch, name="acme", sha="a" * 40, latest_tag="v1.0.0")
    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "pack", "add", "github:acme/acme@latest"])

    assert result.exit_code == 0, result.output
    toml = manager.packs_toml_path(tmp_path).read_text()
    assert 'source = "github:acme/acme@latest"' in toml and "commit" not in toml  # no frozen pin
    (entry,) = manager.read_entries(manager.packs_toml_path(tmp_path))
    assert entry == manager.ExternalPack("acme", manager.PackSource.parse("github:acme/acme@latest"), commit=None)
    meta = manager.PackMeta.load(manager.meta_path("acme"))
    assert meta is not None and meta.commit == "a" * 40  # cache warmed + sidecar recorded


def test_cli_pack_add_tag_freezes_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager.time, "time", Clock())
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha="a" * 40)
    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "pack", "add", "github:acme/acme@v1.2.3"])

    assert result.exit_code == 0, result.output
    toml = manager.packs_toml_path(tmp_path).read_text()
    assert 'source = "github:acme/acme@v1.2.3"' in toml and f'commit = "{"a" * 40}"' in toml  # frozen lockfile pin
    (entry,) = manager.read_entries(manager.packs_toml_path(tmp_path))
    assert entry == manager.ExternalPack("acme", manager.PackSource.parse("github:acme/acme@v1.2.3"), commit="a" * 40)
    assert manager.PackMeta.load(manager.meta_path("acme")) is None  # a frozen pin keeps no sidecar
    assert gh.seen_refs == ["v1.2.3"]  # resolved the tag once, at add time


def test_cli_pack_update_moving_refreshes_sidecar_not_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(manager.time, "time", clock)
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha="a" * 40, latest_tag="v1.0.0")
    enable_moving(tmp_path)
    manager.resolve_enabled_packs(tmp_path)

    gh.sha, gh.tarball = "c" * 40, make_pack_tarball(tmp_path / "src-c", name="acme", top="acme-c")
    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "pack", "update", "acme"])

    assert result.exit_code == 0, result.output
    assert "commit" not in manager.packs_toml_path(tmp_path).read_text()  # still source-only
    meta = manager.PackMeta.load(manager.meta_path("acme"))
    assert meta is not None and meta.commit == "c" * 40  # sidecar advanced to the new commit


def test_cli_pack_update_pinned_repins_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old, new = "a" * 40, "c" * 40
    gh = fake_github(tmp_path, monkeypatch, name="acme", sha=new)
    manager.upsert_entry(
        manager.packs_toml_path(tmp_path),
        manager.ExternalPack("acme", manager.PackSource.parse("github:acme/acme@v1"), old),
    )
    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "pack", "update", "acme"])

    assert result.exit_code == 0, result.output
    (entry,) = manager.read_entries(manager.packs_toml_path(tmp_path))
    assert isinstance(entry, manager.ExternalPack) and entry.commit == new  # re-pinned in the lockfile
    assert gh.seen_refs == ["v1"]  # re-resolved the declared @v1 ref


def test_cli_pack_list_shows_resolved_commit_for_moving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager.time, "time", Clock())
    fake_github(tmp_path, monkeypatch, name="acme", sha="abcdef1" + "0" * 33, latest_tag="v1.0.0")
    enable_moving(tmp_path)
    manager.resolve_enabled_packs(tmp_path)

    listed = CliRunner().invoke(cli, ["--root", str(tmp_path), "pack", "list"])
    assert listed.exit_code == 0, listed.output
    assert "acme" in listed.output and "latest@abcdef1" in listed.output  # honest resolved commit, not "None"


# --- disabled packs.toml entries -----------------------------------------------------


@pytest.mark.parametrize(
    "toml",
    [
        pytest.param("[packs.show]\ndisabled = true\n", id="bare_disabled"),
        # disabled wins even alongside a source (item: disabled beats a packs.toml source)
        pytest.param('[packs.show]\nsource = "github:a/b@v1"\ndisabled = true\n', id="disabled_beats_source"),
    ],
)
def test_parse_entry_disabled(packs_toml: Path, toml: str) -> None:
    packs_toml.write_text(toml)
    assert manager.read_entries(packs_toml) == [manager.DisabledPack("show")]


def test_disabled_entry_round_trips(packs_toml: Path) -> None:
    manager.atomic_write(packs_toml, manager.render_packs_toml([manager.DisabledPack("show")]))
    assert "disabled = true" in packs_toml.read_text()
    assert manager.read_entries(packs_toml) == [manager.DisabledPack("show")]


def test_disabled_pack_is_not_resolved(tmp_path: Path) -> None:
    path = manager.packs_toml_path(tmp_path)
    manager.upsert_entry(path, manager.BuiltinPack("general"))
    path.write_text(path.read_text() + "[packs.python]\ndisabled = true\n")  # decline the python builtin
    resolved, missing = manager.resolve_enabled_packs(tmp_path)
    assert missing == []
    assert [r.entry.name for r in resolved] == ["general"]  # the disabled entry resolves to nothing


# --- attached packs (session-scoped plugin attach) -----------------------------------


def test_read_attached_absent_is_empty(tmp_path: Path) -> None:
    assert manager.read_attached(tmp_path) == []


def test_attached_round_trip_is_keyed_by_name(tmp_path: Path) -> None:
    first = manager.AttachedPack(name="ccx", dir=str(tmp_path / "v1"), version="1.0.0")
    manager.upsert_attached(tmp_path, first)
    assert manager.read_attached(tmp_path) == [first]

    moved = manager.AttachedPack(name="ccx", dir=str(tmp_path / "v2"), version="2.0.0")
    manager.upsert_attached(tmp_path, moved)  # same name replaces, never appends
    assert manager.read_attached(tmp_path) == [moved]

    other = manager.AttachedPack(name="other", dir=str(tmp_path / "o"), version="0.1.0")
    manager.upsert_attached(tmp_path, other)  # a new name appends
    assert manager.read_attached(tmp_path) == [moved, other]


def test_resolve_attached_loads_manifest(tmp_path: Path) -> None:
    pack = write_pack(tmp_path / "p", "ccx", hooks=".")
    session = tmp_path / "session"
    session.mkdir()
    manager.upsert_attached(session, manager.AttachedPack(name="ccx", dir=str(pack), version="0.1.0"))

    (resolved,) = manager.resolve_attached(session)
    assert resolved.entry == manager.AttachedPack(name="ccx", dir=str(pack), version="0.1.0")
    assert resolved.manifest.name == "ccx"
    assert resolved.path == pack  # hooks="." resolves the manifest dir itself


def test_resolve_attached_prunes_stale_dir(tmp_path: Path) -> None:
    pack = write_pack(tmp_path / "p", "ccx", hooks=".")
    session = tmp_path / "session"
    session.mkdir()
    manager.upsert_attached(session, manager.AttachedPack(name="ccx", dir=str(pack), version="0.1.0"))
    assert len(manager.resolve_attached(session)) == 1

    pack.rename(tmp_path / "moved")  # a plugin update moved the versioned cache path
    assert manager.resolve_attached(session) == []  # the dangling entry is silently dropped
