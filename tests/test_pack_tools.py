from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from cc_transcript.tools import expand_tool_names, unregister_mcp_tool

from captain_hook import cli
from captain_hook.cli import CliState
from captain_hook.events import PostToolUseEvent, PreToolUseEvent
from captain_hook.packs import manager, plugins
from captain_hook.packs.general.comments import VerboseComment

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

SYN_SPAN_EDIT = "syn_span_edit"
SYN_GATE = "syn_gate"

PACK_HEAD = '[pack]\nname = "synpack"\ndescription = "d"\nhooks = "hooks"\nversion = "0.1.0"\n\n'
SPAN_EDIT_AND_GATE = (
    "[tools.syn_span_edit]\n"
    'behaves_like = "Edit"\n'
    'span_edit = { path = "path", content = "content", delete = "delete" }\n\n'
    "[tools.syn_gate]\n"
    'behaves_like = "Write"\n'
)
SPAN_EDIT_ONLY = (
    "[tools.syn_span_edit]\n"
    'behaves_like = "Edit"\n'
    'span_edit = { path = "path", content = "content", delete = "delete" }\n'
)

MISSING_BEHAVES_LIKE = '[tools.syn_gate]\nspan_edit = { path = "path", content = "content" }\n'
ENTRY_NOT_A_TABLE = '[tools]\nsyn_gate = "notatable"\n'
NON_STR_SPAN_VALUE = '[tools.syn_span_edit]\nbehaves_like = "Edit"\nspan_edit = { path = 1, content = "content" }\n'
NON_STR_DELETE = '[tools.syn_span_edit]\nbehaves_like = "Edit"\nspan_edit = { path = "p", content = "c", delete = 5 }\n'
SPAN_EDIT_SCALAR = '[tools.syn_span_edit]\nbehaves_like = "Edit"\nspan_edit = "x"\n'


@pytest.fixture(autouse=True)
def isolate(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, isolate_modules: None
) -> Iterator[None]:
    # discover() writes the resolve/plugin sidecars under resolve_cache_dir(); keep them off ~/.cache.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))
    yield
    # cc-transcript's tool registry is a process-global; drop every synthetic registration.
    cli.register_pack_tools([])
    for name in (SYN_SPAN_EDIT, SYN_GATE):
        unregister_mcp_tool(name)


def manifest_file(root: Path, tools_body: str) -> Path:
    (mf := root / manager.PACK_MANIFEST).write_text(PACK_HEAD + tools_body)
    return mf


class TestManifestTools:
    def test_span_edit_and_gate(self, tmp_path: Path) -> None:
        m = manager.PackManifest.load(manifest_file(tmp_path, SPAN_EDIT_AND_GATE))
        assert m.tools == (
            manager.ToolSpec(SYN_SPAN_EDIT, "Edit", manager.SpanEditSpec("path", "content", "delete")),
            manager.ToolSpec(SYN_GATE, "Write"),
        )

    def test_without_span_edit(self, tmp_path: Path) -> None:
        m = manager.PackManifest.load(manifest_file(tmp_path, '[tools.syn_gate]\nbehaves_like = "Write"\n'))
        assert m.tools == (manager.ToolSpec(SYN_GATE, "Write", None),)

    def test_delete_omitted_from_span_edit(self, tmp_path: Path) -> None:
        body = '[tools.syn_span_edit]\nbehaves_like = "Edit"\nspan_edit = { path = "path", content = "content" }\n'
        spec = manager.PackManifest.load(manifest_file(tmp_path, body)).tools[0]
        assert spec.span_edit == manager.SpanEditSpec("path", "content", None)
        assert spec.span_edit.as_map() == {"path": "path", "content": "content"}

    def test_no_tools_table_yields_empty(self, tmp_path: Path) -> None:
        assert manager.PackManifest.load(manifest_file(tmp_path, "")).tools == ()

    @pytest.mark.parametrize(
        ("body", "needle"),
        [
            (MISSING_BEHAVES_LIKE, "missing required string key behaves_like"),
            (ENTRY_NOT_A_TABLE, "must be a table"),
            (NON_STR_SPAN_VALUE, "needs string keys path/content"),
            (NON_STR_DELETE, "span_edit.delete"),
            (SPAN_EDIT_SCALAR, "span_edit in pack 'synpack' must be a table"),
        ],
        ids=[
            "missing-behaves_like",
            "entry-not-a-table",
            "non-str-span-value",
            "non-str-delete",
            "span-edit-scalar",
        ],
    )
    def test_malformed_raises_packerror_naming_pack(self, tmp_path: Path, body: str, needle: str) -> None:
        with pytest.raises(manager.PackError) as exc:
            manager.PackManifest.load(manifest_file(tmp_path, body))
        assert needle in str(exc.value)
        assert "synpack" in str(exc.value)

    def test_top_level_tools_not_a_table_raises(self, tmp_path: Path) -> None:
        # A top-level scalar key must precede the first table header, so build the file directly.
        mf = tmp_path / manager.PACK_MANIFEST
        mf.write_text("tools = 5\n" + PACK_HEAD)
        with pytest.raises(manager.PackError) as exc:
            manager.PackManifest.load(mf)
        assert "must be a table of tool entries" in str(exc.value)
        assert "synpack" in str(exc.value)


def plant_installed() -> None:
    (path := plugins.installed_plugins_path()).parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")


def write_snapshot(root: Path, roster: Sequence[tuple[str, Path]]) -> None:
    (path := plugins.snapshot_path(root)).parent.mkdir(parents=True, exist_ok=True)
    plugins.PluginSnapshot(
        stat=plugins.stat_records(root),
        plugins=tuple(plugins.EnabledPlugin(id=pid, version="1.0.0", root=str(proot)) for pid, proot in roster),
    ).write(path)


def make_project(root: Path) -> CliState:
    (hooks := root / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    (hooks / "h.py").write_text("from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='m')\n")
    return CliState(root=root, hooks=str(hooks))


def write_plugin_pack(pack_root: Path, tools_body: str) -> None:
    (hooks := pack_root / "hooks").mkdir(parents=True, exist_ok=True)
    (hooks / "conf.py").write_text("from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='pp')\n")
    (pack_root / manager.PACK_MANIFEST).write_text(PACK_HEAD + tools_body)


def enable_plugin_pack(tmp_path: Path, tools_body: str) -> CliState:
    """A project whose one enabled plugin ships a ``[tools]`` manifest, discoverable with no live claude."""
    state = make_project(root := tmp_path / "proj")
    write_plugin_pack(pack_root := tmp_path / "plug", tools_body)
    plant_installed()
    write_snapshot(root, [("acme/synpack", pack_root)])
    return state


def test_discover_registers_and_unregisters_pack_tools(tmp_path: Path) -> None:
    state = enable_plugin_pack(tmp_path, SPAN_EDIT_AND_GATE)

    state.discover()
    assert SYN_SPAN_EDIT in expand_tool_names("Edit")
    assert SYN_GATE in expand_tool_names("Write")

    write_plugin_pack(tmp_path / "plug", SPAN_EDIT_ONLY)
    write_snapshot(tmp_path / "proj", [("acme/synpack", tmp_path / "plug")])
    state.discover()
    assert SYN_SPAN_EDIT in expand_tool_names("Edit")
    assert SYN_GATE not in expand_tool_names("Write")


def test_reconcile_pack_tools_touches_only_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[str] = []
    unregistered: list[str] = []
    monkeypatch.setattr(cli, "register_mcp_tool", lambda name, behaves, span: registered.append(name))
    monkeypatch.setattr(cli, "unregister_mcp_tool", lambda name: unregistered.append(name))

    first = {"tool_a": ("Edit", None), "tool_b": ("Write", {"path": "p", "content": "c"})}
    cli.reconcile_pack_tools(first)
    assert sorted(registered) == ["tool_a", "tool_b"]
    registered.clear()

    # A steady-state rebuild with the identical map is a strict no-op — no live spec blinks out.
    cli.reconcile_pack_tools(dict(first))
    assert registered == [] and unregistered == []

    # Only the changed (tool_b), added (tool_c), and removed (tool_a) tools are touched; tool_b
    # re-registers with no unregister first (last write wins).
    second = {"tool_b": ("Write", {"path": "p", "content": "c", "delete": "d"}), "tool_c": ("Edit", None)}
    cli.reconcile_pack_tools(second)
    assert sorted(registered) == ["tool_b", "tool_c"]
    assert unregistered == ["tool_a"]


def pre_tool_use(target: Path, content: str | None, *, delete: bool = False) -> PreToolUseEvent:
    payload: dict[str, object] = {"path": str(target)}
    if delete:
        payload["delete"] = True
    else:
        payload["content"] = content
    return PreToolUseEvent(_raw={"tool_name": "mcp__x__syn_span_edit", "tool_input": payload}, ctx=MagicMock())


def test_span_edit_fires_verbose_comment_end_to_end(tmp_path: Path) -> None:
    enable_plugin_pack(tmp_path, SPAN_EDIT_ONLY).discover()

    target = tmp_path / "edited.py"
    target.write_text("x = 1\n")
    long_run = "# c1\n# c2\n# c3\n# c4\n# c5\n# c6\ny = 2\n"
    evt = pre_tool_use(target, long_run)

    # A registered span edit yields the whole-file pre-image and the new span text, no post-image.
    assert evt.file is not None and evt.file.path == target
    assert evt.content == long_run
    assert evt.replaced == "x = 1\n"
    assert evt.pre_image == "x = 1\n"
    assert evt.post_image is None

    # The real hook condition fires through touched()'s span-edit fallback — this is the litmus:
    # reverting that fallback makes touched() return [] on a post-image-less event and flips this to False.
    assert VerboseComment().check(evt) is True
    # A short run stays under budget.
    assert VerboseComment().check(pre_tool_use(target, "# just one line\ny = 2\n")) is False
    # A deletion carries no new content, so nothing is introduced.
    deleted = pre_tool_use(target, None, delete=True)
    assert deleted.content is None
    assert VerboseComment().check(deleted) is False
    # PostToolUse has no pre-image (replaced is None off PreToolUse), so the fallback yields no fire.
    assert VerboseComment().check(PostToolUseEvent(_raw=evt._raw, ctx=MagicMock())) is False

    # The same long run already on disk is suppressed: the block is present in the pre-image, so it
    # is neither created nor grown — the conservative superset never false-positives on it.
    present = tmp_path / "already.py"
    present.write_text(long_run)
    assert VerboseComment().check(pre_tool_use(present, long_run)) is False


def test_doomed_builtin_edit_stays_out_of_span_fallback(tmp_path: Path) -> None:
    enable_plugin_pack(tmp_path, SPAN_EDIT_ONLY).discover()

    target = tmp_path / "edited.py"
    target.write_text("x = 1\n")
    long_run = "# c1\n# c2\n# c3\n# c4\n# c5\n# c6\ny = 2\n"
    evt = PreToolUseEvent(
        _raw={
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target), "old_string": "not in the file", "new_string": long_run},
        },
        ctx=MagicMock(),
    )

    # The failed simulation leaves no post-image, but a builtin edit's `replaced` is just its old
    # span — not a whole-file superset — so the span fallback must stay out and nothing fires.
    assert evt.post_image is None
    assert evt.replaced == "not in the file"
    assert VerboseComment().check(evt) is False
