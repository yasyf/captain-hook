from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from cc_transcript.tools import expand_tool_names, unregister_mcp_tool

from captain_hook import ConfirmVerdict, Event, cli
from captain_hook.builtin_packs.general.hooks.comments import VerboseComment
from captain_hook.cli import CliState
from captain_hook.dispatch import execute_hook
from captain_hook.events import PostToolUseEvent, PreToolUseEvent
from captain_hook.packs import manager
from captain_hook.types import Action, HookSpec, RegisteredHook
from tests.helpers import make_ctx, plant_roster
from tests.helpers import make_project as scaffold

if TYPE_CHECKING:
    from collections.abc import Iterator

    from captain_hook.context import HookContext

SYN_SPAN_EDIT = "syn_span_edit"
SYN_GATE = "syn_gate"

DESCRIPTOR_HEAD = "resources = []\n\n"
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
    (mf := root / manager.PACK_DESCRIPTOR).write_text(DESCRIPTOR_HEAD + tools_body)
    return mf


class TestManifestTools:
    def test_span_edit_and_gate(self, tmp_path: Path) -> None:
        m = manager.PackDescriptor.load(manifest_file(tmp_path, SPAN_EDIT_AND_GATE))
        assert m.tools == (
            manager.ToolSpec(SYN_SPAN_EDIT, "Edit", manager.SpanEditSpec("path", "content", "delete")),
            manager.ToolSpec(SYN_GATE, "Write"),
        )

    def test_without_span_edit(self, tmp_path: Path) -> None:
        m = manager.PackDescriptor.load(manifest_file(tmp_path, '[tools.syn_gate]\nbehaves_like = "Write"\n'))
        assert m.tools == (manager.ToolSpec(SYN_GATE, "Write", None),)

    def test_delete_omitted_from_span_edit(self, tmp_path: Path) -> None:
        body = '[tools.syn_span_edit]\nbehaves_like = "Edit"\nspan_edit = { path = "path", content = "content" }\n'
        spec = manager.PackDescriptor.load(manifest_file(tmp_path, body)).tools[0]
        assert spec.span_edit == manager.SpanEditSpec("path", "content", None)
        assert spec.span_edit.as_map() == {"path": "path", "content": "content"}

    def test_no_tools_table_yields_empty(self, tmp_path: Path) -> None:
        assert manager.PackDescriptor.load(manifest_file(tmp_path, "")).tools == ()

    @pytest.mark.parametrize(
        ("body", "needle"),
        [
            (MISSING_BEHAVES_LIKE, "missing required string key behaves_like"),
            (ENTRY_NOT_A_TABLE, "must be a table"),
            (NON_STR_SPAN_VALUE, "needs string keys path/content"),
            (NON_STR_DELETE, "span_edit.delete"),
            (SPAN_EDIT_SCALAR, "span_edit in"),
        ],
        ids=[
            "missing-behaves_like",
            "entry-not-a-table",
            "non-str-span-value",
            "non-str-delete",
            "span-edit-scalar",
        ],
    )
    def test_malformed_raises_packerror_naming_descriptor(self, tmp_path: Path, body: str, needle: str) -> None:
        with pytest.raises(manager.PackError) as exc:
            manager.PackDescriptor.load(manifest_file(tmp_path, body))
        assert needle in str(exc.value)
        assert str(tmp_path) in str(exc.value)  # the descriptor path labels the error

    def test_top_level_tools_not_a_table_raises(self, tmp_path: Path) -> None:
        with pytest.raises(manager.PackError) as exc:
            manager.PackDescriptor.load(manifest_file(tmp_path, "tools = 5\n"))
        assert "must be a table of tool entries" in str(exc.value)


def make_project(root: Path) -> CliState:
    scaffold(root, "from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='m')\n")
    return CliState(root=root, hooks=str(root / ".claude" / "hooks"))


def write_plugin_pack(pack_root: Path, tools_body: str) -> None:
    pack = pack_root / manager.PLUGIN_PACK_DIRNAME
    (hooks := pack / manager.HOOKS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (hooks / "guard.py").write_text("from captain_hook import Event, hook\n\nhook(Event.PreToolUse, message='pp')\n")
    (pack / manager.PACK_DESCRIPTOR).write_text(DESCRIPTOR_HEAD + tools_body)


def enable_plugin_pack(tmp_path: Path, tools_body: str) -> CliState:
    """A project whose one enabled plugin ships a ``[tools]`` manifest, discoverable with no live claude."""
    state = make_project(tmp_path / "proj")
    write_plugin_pack(pack_root := tmp_path / "plug", tools_body)
    plant_roster([("acme/synpack", pack_root)])
    return state


def test_discover_registers_and_unregisters_pack_tools(tmp_path: Path) -> None:
    state = enable_plugin_pack(tmp_path, SPAN_EDIT_AND_GATE)

    state.discover()
    assert SYN_SPAN_EDIT in expand_tool_names("Edit")
    assert SYN_GATE in expand_tool_names("Write")

    write_plugin_pack(tmp_path / "plug", SPAN_EDIT_ONLY)
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


def resolved_with_tool(entry: manager.PackEntry, tool: str) -> manager.ResolvedPack:
    return manager.ResolvedPack(entry, Path("/x"), manager.PackDescriptor(tools=(manager.ToolSpec(tool, "Edit"),)))


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(manager.BuiltinPack("general"), manager.PluginPack("cc@mkt", "/root"), id="builtin-plus-plugin"),
        pytest.param(manager.PluginPack("a@mkt", "/ra"), manager.PluginPack("b@mkt", "/rb"), id="plugin-plus-plugin"),
    ],
)
def test_pack_tool_specs_rejects_cross_pack_duplicate(first: manager.PackEntry, second: manager.PackEntry) -> None:
    a, b = resolved_with_tool(first, "dup_tool"), resolved_with_tool(second, "dup_tool")
    with pytest.raises(manager.PackError) as exc:
        cli.pack_tool_specs([a, b])
    msg = str(exc.value)
    assert "dup_tool" in msg and a.pack_id in msg and b.pack_id in msg


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


def test_tooling_refusal_reads_structured_tool_responses(tmp_path: Path) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import refusal
    from captain_hook.context import HookContext
    from captain_hook.session import SessionStore

    response = {"stdout": "", "stderr": "cc-slack: no cc-slack session for this Claude window\n", "interrupted": False}
    evt = PostToolUseEvent(
        _raw={
            "tool_name": "Bash",
            "tool_input": {"command": "cc-slack reply --url C1/p12 --text hi"},
            "tool_response": response,
        },
        ctx=HookContext(SessionStore(tmp_path), None, None),
    )
    found = refusal(evt)
    assert found is not None
    key, record = found
    assert key == "cc-slack-session"
    assert record.evidence == "cc-slack: no cc-slack session for this Claude window"


def test_tooling_refusal_drops_a_pre_expiry_record_and_spares_unrelated_dispatches(tmp_path: Path) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import (
        CC_SLACK_CLI_SYNC,
        ToolingRefusals,
        block_repeated_dispatch,
    )
    from captain_hook.context import HookContext
    from captain_hook.session import SessionStore

    store = SessionStore(tmp_path)
    slot = store[ToolingRefusals].path
    assert slot is not None
    slot.write_text(
        '{"refusals": {"github-quota": {"tool": "ccx", "action": "\\\\bccx vcs\\\\b", "evidence": "x", "lane": false}}}'
    )
    evt = PreToolUseEvent(
        _raw={"tool_name": "Agent", "tool_input": {"prompt": CC_SLACK_CLI_SYNC, "name": "cc-slack-cli-sync"}},
        ctx=HookContext(store, None, None),
    )

    assert ToolingRefusals.load(evt).refusals == {}
    assert block_repeated_dispatch(evt) is None


def test_tooling_lane_spawned_before_the_refusal_silences_it(tmp_path: Path) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import block_repeated_dispatch, record_lane, record_refusal
    from captain_hook.context import HookContext
    from captain_hook.session import SessionStore

    ctx = HookContext(SessionStore(tmp_path), None, None)
    spawn = PreToolUseEvent(
        _raw={"tool_name": "Agent", "tool_input": {"prompt": "Fix ccx reads.", "name": "gh-quota-once-and-for-all"}},
        ctx=ctx,
    )
    refused = PostToolUseEvent(
        _raw={
            "tool_name": "Bash",
            "tool_input": {"command": "ccx vcs pr status 12"},
            "tool_response": {"stdout": "", "stderr": "ccx: GitHub GraphQL quota exhausted\n"},
        },
        ctx=ctx,
    )
    repeat = PreToolUseEvent(
        _raw={"tool_name": "Agent", "tool_input": {"prompt": "Poll `ccx vcs pr status 12` until it lands."}},
        ctx=ctx,
    )

    assert record_lane(spawn) is None
    assert record_refusal(refused) is None
    assert block_repeated_dispatch(repeat) is None


TOOLING_FIXTURES = Path(__file__).parent / "fixtures" / "tooling"


def dispatch(ctx: object, prompt: str, name: str) -> PreToolUseEvent:
    return PreToolUseEvent(_raw={"tool_name": "Agent", "tool_input": {"prompt": prompt, "name": name}}, ctx=ctx)


def repeat_guard() -> RegisteredHook:
    from captain_hook.builtin_packs.general.hooks.tooling import block_repeated_dispatch

    return RegisteredHook(spec=HookSpec(events=Event.PreToolUse), handler=block_repeated_dispatch, name="repeat")


def judging(tmp_path: Path, refusals: dict[str, object], **verdict: bool) -> HookContext:
    from captain_hook.builtin_packs.general.hooks.tooling import ToolingRefusals

    ctx = make_ctx(tmp_path)
    ctx.call_llm = MagicMock(return_value=ConfirmVerdict(reasoning="r", **verdict))  # type: ignore[method-assign]
    ctx.session[ToolingRefusals].set(ToolingRefusals(refusals=refusals))
    return ctx


def test_the_cc_slack_cli_sync_dispatch_passes_the_stale_quota_refusal_without_the_model(tmp_path: Path) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import QUOTA

    ctx = judging(tmp_path, {"github-quota": QUOTA}, block=True, confident=True)
    prompt = (TOOLING_FIXTURES / "cc_slack_cli_sync.txt").read_text()

    assert execute_hook(repeat_guard(), dispatch(ctx, prompt, "cc-slack-cli-sync")) is None
    ctx.call_llm.assert_not_called()


@pytest.mark.parametrize(
    ("verdict", "action", "message"),
    [
        ({"block": False, "confident": True}, None, None),
        ({"block": True, "confident": True}, Action.block, "already refused"),
    ],
)
def test_the_cc_slack_cli_sync_dispatch_blocks_only_on_a_confident_match(
    tmp_path: Path, verdict: dict[str, bool], action: Action | None, message: str | None
) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import SLACK_REFUSED

    ctx = judging(tmp_path, dict(SLACK_REFUSED.refusals), **verdict)
    prompt = (TOOLING_FIXTURES / "cc_slack_cli_sync.txt").read_text()

    result = execute_hook(repeat_guard(), dispatch(ctx, prompt, "cc-slack-cli-sync"))

    assert (result and result.action) is action
    assert message is None or message in (result.message or "")
    judged = str(ctx.call_llm.call_args.args[0])
    assert "the same action the first-party tool refused" in judged
    assert "no cc-slack session for this Claude window" in judged


def test_a_foreign_raw_heredoc_never_blocks_the_recon_dispatch(tmp_path: Path) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import ToolingRefusals, record_refusal

    ctx = judging(tmp_path, {}, block=True, confident=True)
    foreign = PostToolUseEvent(
        _raw={
            "tool_name": "Bash",
            "tool_input": {
                "command": "cat > ~/.claude/worktrees/captain-hook/scratch/tnf/tail.py <<A <<'PY'\n"
                "LIVE = datetime(2099, 1, 1, tzinfo=UTC)\nA\n"
                "RESET_PASSED = datetime(2026, 1, 1, tzinfo=UTC)  # ccx:raw\nPY\n"
                "gh pr checks 236 --repo yasyf/captain-hook"
            },
            "tool_response": {"stdout": "", "stderr": ""},
        },
        ctx=ctx,
    )
    prompt = (TOOLING_FIXTURES / "aig_bucket_cutover_recon.txt").read_text()

    assert record_refusal(foreign) is None
    assert ToolingRefusals.load(foreign).refusals == {}
    assert execute_hook(repeat_guard(), dispatch(ctx, prompt, "aig-bucket-cutover-recon")) is None
    ctx.call_llm.assert_not_called()


def test_a_session_wide_raw_env_records_no_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import refusal

    monkeypatch.setenv("CAPT_HOOK_CCX_RAW", "1")
    evt = PostToolUseEvent(
        _raw={"tool_name": "Bash", "tool_input": {"command": "gh pr edit 9 --base dev"}, "tool_response": {}},
        ctx=make_ctx(tmp_path),
    )

    assert "raw" in evt.annotations
    assert refusal(evt) is None


def test_a_quota_phrase_in_printed_prose_records_no_refusal(tmp_path: Path) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import PR_236_BODY, refusal

    evt = PostToolUseEvent(
        _raw={
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr view 236 --repo yasyf/captain-hook --json body -q .body"},
            "tool_response": {"stdout": PR_236_BODY, "stderr": "", "interrupted": False},
        },
        ctx=make_ctx(tmp_path),
    )

    assert refusal(evt) is None


@pytest.mark.parametrize(
    ("command", "error"),
    [
        (
            "ccx vcs pr status 12",
            "Exit code 1\nccx: GitHub GraphQL quota exhausted; rate-limited until 2099-01-01T00:00:00Z",
        ),
        ("gh pr view 12 --json state", "Exit code 1\nGraphQL: API rate limit exceeded for user ID 1"),
    ],
)
def test_a_failed_github_call_records_the_quota_refusal(tmp_path: Path, command: str, error: str) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import ToolingRefusals, record_refusal
    from captain_hook.events import PostToolUseFailureEvent

    evt = PostToolUseFailureEvent(
        _raw={"tool_name": "Bash", "tool_input": {"command": command}, "error": error}, ctx=make_ctx(tmp_path)
    )

    result = record_refusal(evt)

    assert result is not None
    assert "`ccx: tooling-lane=github-quota`" in (result.message or "")
    assert set(ToolingRefusals.load(evt).refusals) == {"github-quota"}


def test_a_failed_github_call_under_a_live_lane_stays_quiet(tmp_path: Path) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import ToolingRefusals, record_refusal
    from captain_hook.events import PostToolUseFailureEvent

    ctx = make_ctx(tmp_path)
    ctx.session[ToolingRefusals].set(ToolingRefusals(lanes={"github-quota"}))
    evt = PostToolUseFailureEvent(
        _raw={
            "tool_name": "Bash",
            "tool_input": {"command": "ccx vcs pr status 12"},
            "error": "Exit code 1\nccx: GitHub GraphQL quota exhausted",
        },
        ctx=ctx,
    )

    assert record_refusal(evt) is None


@pytest.mark.parametrize(
    "command",
    [
        "cd /Users/yasyf/.claude/worktrees/monorepo/cc-slack-reexec-newest && git rebase origin/dev 2>&1 | tail -5; "
        "git status -s | head # ccx:raw",
        "gh pr edit 9 --base dev  # ccx:raw",
        "cd wt && gt submit --no-interactive  # ccx:raw",
    ],
)
def test_a_real_raw_comment_records_and_arms_nothing(tmp_path: Path, command: str) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import ToolingRefusals, record_refusal

    ctx = judging(tmp_path, {}, block=True, confident=True)
    raw = PostToolUseEvent(
        _raw={"tool_name": "Bash", "tool_input": {"command": command}, "tool_response": {"stdout": "", "stderr": ""}},
        ctx=ctx,
    )
    prompt = "Rebase onto dev with `git rebase origin/dev`, retarget with `gh pr edit 9 --base dev`, then `gt submit`."

    assert "raw" in raw.annotations
    assert record_refusal(raw) is None
    assert ToolingRefusals.load(raw).refusals == {}
    assert execute_hook(repeat_guard(), dispatch(ctx, prompt, "rebase-and-retarget")) is None
    ctx.call_llm.assert_not_called()


def test_a_failed_ccx_refusal_blocks_a_confident_repeat_dispatch(tmp_path: Path) -> None:
    from captain_hook.builtin_packs.general.hooks.tooling import ToolingRefusals, record_refusal
    from captain_hook.events import PostToolUseFailureEvent

    ctx = judging(tmp_path, {}, block=True, confident=True)
    refused = PostToolUseFailureEvent(
        _raw={
            "tool_name": "Bash",
            "tool_input": {"command": "ccx vcs pr status 12"},
            "error": "Exit code 1\nccx: GitHub GraphQL quota exhausted; rate-limited until 2099-01-01T00:00:00Z",
        },
        ctx=ctx,
    )

    assert record_refusal(refused) is not None
    assert set(ToolingRefusals.load(refused).refusals) == {"github-quota"}
    result = execute_hook(repeat_guard(), dispatch(ctx, "Poll `ccx vcs pr status 12` until it lands.", "pr-12-watch"))
    assert result is not None
    assert result.action is Action.block
    assert "`ccx: tooling-lane=github-quota`" in (result.message or "")
