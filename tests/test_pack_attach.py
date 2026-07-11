from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cc_transcript.ids import SessionId

from captain_hook.cli import CliState
from captain_hook.packs import manager
from captain_hook.session import ensure_session
from tests.helpers import run_cli

HOOK_TMPL = "from captain_hook import Event, hook\n\nhook(Event.{event}, message={msg!r}{extra})\n"


def write_pack(
    root: Path, name: str, *, event: str = "PreToolUse", msg: str = "m", block: bool = False, nlp: bool = False
) -> Path:
    """A flat single-file pack (``hooks = "."``) whose one hook subscribes ``event``."""
    root.mkdir(parents=True, exist_ok=True)
    manifest = f'name = "{name}"\nversion = "0.1.0"\ndescription = "d"\nhooks = "."\n'
    if nlp:
        manifest += "nlp = true\n"
    (root / manager.PACK_MANIFEST).write_text(manifest)
    (root / "h.py").write_text(HOOK_TMPL.format(event=event, msg=msg, extra=", block=True" if block else ""))
    return root


def attach(session_id: str, pack: Path) -> None:
    manager.upsert_attached(
        ensure_session(SessionId(session_id)),
        manager.AttachedPack(
            name=manager.PackManifest.load(manager.manifest_in(pack)).name, dir=str(pack), version="0.1.0"
        ),
    )


def discover(root: Path, session_id: str) -> list[manager.ResolvedPack]:
    return CliState(root=root, hooks=str(root / ".claude" / "hooks")).discover(
        session_dir=ensure_session(SessionId(session_id))
    )


# --- pack attach CLI -----------------------------------------------------------------


def test_cli_attach_records_and_writes_no_stdout(tmp_path: Path) -> None:
    pack = write_pack(tmp_path / "ccx", "ccx")
    result = run_cli("pack", "attach", str(pack), stdin_data=json.dumps({"session_id": "sess-1"}))

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""  # SessionStart stdout is injected into the model context — stay silent
    recorded = manager.read_attached(ensure_session(SessionId("sess-1")))
    assert recorded == [manager.AttachedPack(name="ccx", dir=str(pack.resolve()), version="0.1.0")]


def test_cli_attach_invalid_manifest_exits_1(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run_cli("pack", "attach", str(empty), stdin_data=json.dumps({"session_id": "sess-1"}))

    assert result.returncode == 1
    assert manager.PACK_MANIFEST in result.stderr  # plugin-author programmer error fails loud on stderr
    assert result.stdout == ""


def test_concurrent_attach_all_land(tmp_path: Path) -> None:
    # Two plugins' SessionStart hooks run `pack attach` in parallel against one session.
    # With per-call temp names and a file-locked read-modify-write, every entry must land
    # and no writer may crash on a temp file the other already consumed.
    n = 6
    packs = [write_pack(tmp_path / f"p{i}", f"p{i}") for i in range(n)]
    stdin = json.dumps({"session_id": "sess-conc"})
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda p: run_cli("pack", "attach", str(p), stdin_data=stdin), packs))

    assert all(r.returncode == 0 for r in results), [r.stderr for r in results if r.returncode != 0]
    recorded = {p.name for p in manager.read_attached(ensure_session(SessionId("sess-conc")))}
    assert recorded == {f"p{i}" for i in range(n)}  # no writer clobbered another's entry


def test_upsert_attached_same_name_new_dir_logs(tmp_path: Path, logcap) -> None:  # type: ignore[no-untyped-def]
    session_dir = ensure_session(SessionId("sess-1"))
    first, second = write_pack(tmp_path / "a", "x"), write_pack(tmp_path / "b", "x")
    manager.upsert_attached(session_dir, manager.AttachedPack(name="x", dir=str(first), version="0.1.0"))
    manager.upsert_attached(session_dir, manager.AttachedPack(name="x", dir=str(second), version="0.1.0"))

    assert manager.read_attached(session_dir) == [manager.AttachedPack(name="x", dir=str(second), version="0.1.0")]
    rebind = [r for r in logcap.records if "re-bound" in r.message]
    assert len(rebind) == 1  # only the differing-dir upsert logs, not the initial attach
    assert str(first) in rebind[0].message and str(second) in rebind[0].message  # both dirs named


def test_upsert_attached_same_name_same_dir_is_silent(tmp_path: Path, logcap) -> None:  # type: ignore[no-untyped-def]
    session_dir = ensure_session(SessionId("sess-1"))
    pack = write_pack(tmp_path / "a", "x")
    entry = manager.AttachedPack(name="x", dir=str(pack), version="0.1.0")
    manager.upsert_attached(session_dir, entry)
    manager.upsert_attached(session_dir, entry)  # re-attach of the same dir (a plain re-run)

    assert manager.read_attached(session_dir) == [entry]
    assert not [r for r in logcap.records if "re-bound" in r.message]  # same dir: no collision warning


# --- discover precedence -------------------------------------------------------------


@pytest.mark.parametrize(
    ("packs_toml", "attach_name", "attach_wins"),
    [
        # a packs.toml builtin of the same name wins over the ambient attach
        pytest.param("[packs.general]\n", "general", False, id="packs_toml_beats_attached"),
        # `disabled = true` declines the attach without pinning anything (item 5)
        pytest.param("[packs.general]\ndisabled = true\n", "general", False, id="disabled_beats_attached"),
        # an unclaimed name loads from the attach
        pytest.param("", "extra", True, id="unclaimed_attach_loads"),
    ],
)
def test_discover_precedence(tmp_path: Path, packs_toml: str, attach_name: str, attach_wins: bool) -> None:
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    if packs_toml:
        manager.packs_toml_path(tmp_path).write_text(packs_toml)
    attach("sess-1", write_pack(tmp_path / "ambient", attach_name))

    resolved = discover(tmp_path, "sess-1")
    from_attach = [r for r in resolved if isinstance(r.entry, manager.AttachedPack) and r.entry.name == attach_name]
    assert bool(from_attach) is attach_wins
    if not attach_wins:
        # the name is either a resolved builtin (packs.toml) or resolves to nothing (disabled)
        assert all(not isinstance(r.entry, manager.AttachedPack) for r in resolved)


def test_disabled_beats_both_sources(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    manager.packs_toml_path(tmp_path).write_text('[packs.show]\nsource = "github:a/b@v1"\ndisabled = true\n')
    attach("sess-1", write_pack(tmp_path / "ambient", "show"))

    resolved = discover(tmp_path, "sess-1")
    assert all(r.entry.name != "show" for r in resolved)  # disabled wins over source and attach alike


@pytest.mark.parametrize(
    ("manifest", "make_file"),
    [
        pytest.param("name = = broken", True, id="malformed_toml"),
        pytest.param('version = "0.1.0"\ndescription = "d"\nhooks = "."\n', True, id="missing_name_key"),
        pytest.param("", False, id="manifest_absent"),
    ],
)
def test_discover_skips_attached_pack_with_bad_manifest(tmp_path: Path, manifest: str, make_file: bool) -> None:
    # A plugin auto-update can replace the pack dir non-atomically mid-session, leaving a
    # missing/partial manifest. That attach must be dropped fail-soft, not raised out of
    # discover() where it would kill dispatch for every other hook in the event.
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    attach("sess-1", write_pack(tmp_path / "good", "good"))
    bad = tmp_path / "bad"
    bad.mkdir()
    if make_file:
        (bad / manager.PACK_MANIFEST).write_text(manifest)
    # Record the attach directly: the CLI would reject a bad manifest at attach time, but the
    # dir can rot afterwards, which is exactly the mid-session case under test.
    manager.upsert_attached(
        ensure_session(SessionId("sess-1")), manager.AttachedPack(name="bad", dir=str(bad), version="0.1.0")
    )

    resolved = discover(tmp_path, "sess-1")
    names = {r.entry.name for r in resolved}
    assert "good" in names  # the healthy attach still loads
    assert "bad" not in names  # the broken attach is skipped, not fatal


# --- end-to-end run ------------------------------------------------------------------


def run_pretool(session_id: str, root: Path, **extra: object) -> object:
    stdin = json.dumps({"session_id": session_id, "tool_name": "Bash", "tool_input": {"command": "echo hi"}, **extra})
    return run_cli("run", "PreToolUse", root_dir=str(root), stdin_data=stdin)


def test_e2e_attached_hook_fires_exactly_once(tmp_path: Path) -> None:
    pack = write_pack(tmp_path / "ccx", "ccx", msg="ATTACHED_FIRED", block=True)
    attached = run_cli("pack", "attach", str(pack), stdin_data=json.dumps({"session_id": "sess-1"}))
    assert attached.returncode == 0, attached.stderr

    fired = run_pretool("sess-1", tmp_path)
    assert fired.returncode == 0, fired.stderr
    assert fired.stdout.count("ATTACHED_FIRED") == 1  # discovered once, dispatched once

    # a different session never attached the pack, so its hook stays silent
    silent = run_pretool("other-sess", tmp_path)
    assert "ATTACHED_FIRED" not in silent.stdout


def test_e2e_subagent_shares_parent_session_and_loads_attach(tmp_path: Path) -> None:
    # A subagent (sidechain) hook event carries the SAME session_id as its parent — the Claude
    # session UUID is the single session key, and subagent transcripts nest under it as
    # <session>/subagents/agent-<tool_use_id>.jsonl. So an attach written at SessionStart is
    # found for subagent events too (they set agent_id, never a different session_id).
    pack = write_pack(tmp_path / "ccx", "ccx", msg="ATTACHED_FIRED", block=True)
    attached = run_cli("pack", "attach", str(pack), stdin_data=json.dumps({"session_id": "parent-sess"}))
    assert attached.returncode == 0, attached.stderr

    subagent = run_pretool("parent-sess", tmp_path, agent_id="sub-1")
    assert subagent.returncode == 0, subagent.stderr
    assert subagent.stdout.count("ATTACHED_FIRED") == 1  # subagent shares the parent session -> attach loads
