from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from cc_transcript import parse_events_from_bytes
from cc_transcript.context import SUMMARY_LABEL, ContextWindow, TurnRef
from cc_transcript.decisions import Decision
from cc_transcript.ids import EventRef, EventUuid, SessionId, ToolDigest, tool_digest
from cc_transcript.judge.verdicts import GoldenRow, golden_result
from cc_transcript.mining.candidates import DedupKey, dedup_key
from cc_transcript.mining.confidence import HIGH, MEDIUM, VERY_HIGH

from captain_hook.decisions import decisions_db_path, open_decision_log
from captain_hook.packs import manager, plugins
from captain_hook.review.fix import (
    COMPLIANCE_RE,
    HOOK_COMPLAINT,
    classify_marker,
    external_target_path,
    fingerprint_of,
    iter_hook_complaint_signals,
    named_hook_target,
    resolve_target,
)
from captain_hook.review.judge import ReviewVerdict, build_prompt
from captain_hook.review.repo import RepoKey
from captain_hook.review.routing import CAPTAIN_HOOK_REPO, ExternalRoute, PackIndex, Target
from captain_hook.review.scan import ScanReport, scan_transcript
from captain_hook.review.settings import ReviewSettings
from captain_hook.review.store import ReviewStore
from tests.review_helpers import (
    assistant_text,
    assistant_tool_use,
    envelope,
    tool_result,
    user_text,
    write_transcript,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cc_transcript.decisions import DecisionLog
    from cc_transcript.models import TranscriptEvent

    from captain_hook.review.judge import Category

FIXTURES = Path(__file__).parent / "fixtures" / "hook_fires"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
MISFIRE_FIXTURE = "fire-misfire-complaint.jsonl"
REPO = RepoKey("github.com/yasyf/scratch")
BASE_TS = "2026-06-01T12:00:00+00:00"
BASE_MS = int(datetime.fromisoformat(BASE_TS).timestamp() * 1000)
PRIMITIVE_NUDGE = "/x/site-packages/captain_hook/primitives/nudge.py"
NUDGE_MESSAGE = "Remember to use the project's task tracker before running status checks."
STOP_MESSAGE = "Before you finish: leave a one-line summary of what changed."
STRONG_COMPLAINT = "**Note**: The task tracker reminder re-fired on a sequence I already completed - ignoring it."
HEDGED_COMPLAINT = "The lint reminder seems to have misfired here - the file is generated output, not source."
STOP_COMPLAINT = "That stop gate shouldn't have fired - I had already addressed every open task"
TASK_TRACKING_COMPLAINT = (
    "The task-tracking hook keeps re-firing even though I've been updating the tracker - this is a misfire."
)
GIT_STATUS_DIGEST = tool_digest("Bash", {"command": "git status"})
INDEX = PackIndex.load(None)
NOTIFY_REPO = RepoKey("github.com/acme/notify-hooks")
NOTIFY_KIND = "notify.alerts:hook_deadbeef"
NOTIFY_SOURCE = "/home/u/.cache/captain-hook/packs/notify@abc1234/hooks/alerts.py"


def cache_external_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str = "notify", sha: str = "abc1234"
) -> Path:
    monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
    cached = tmp_path / "cache" / f"{name}@{sha}"
    cached.mkdir(parents=True)
    (cached / manager.SHA_MARKER).write_text(sha)
    return cached


def write_external_packs_toml(
    root: Path, *, name: str = "notify", source: str = "github:acme/notify-hooks@v1", commit: str = "abc1234"
) -> Path:
    path = manager.config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[packs.{name}]\nsource = "{source}"\ncommit = "{commit}"\n')
    return root


def plant_plugin_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    name: str = "notify",
    plugin_id: str = "acme@notify",
) -> Path:
    """Plant a discovered plugin pack for ``root``: a ``[pack]`` dir plus a read-only snapshot naming it."""
    monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
    plugin_dir = tmp_path / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / manager.PACK_MANIFEST).write_text(f'[pack]\nname = "{name}"\ndescription = "d"\nhooks = "."\n')
    plugins.PluginSnapshot(
        stat=(), plugins=(plugins.EnabledPlugin(id=plugin_id, version="1.0.0", root=str(plugin_dir)),)
    ).write(plugins.snapshot_path(root))
    return plugin_dir


@dataclass(frozen=True, slots=True)
class Verdict:
    accepted: bool = True
    confidence: float = 0.9
    category: str = "misfire_confirmed"
    summary: str = "claude dismissed the fire"
    rationale: str = "explicit dismissal"
    canonical_key: str | None = None


def fixture_events(name: str) -> list[TranscriptEvent]:
    return parse_events_from_bytes((FIXTURES / name).read_bytes())


async def seed_fixture_decisions(decisions: DecisionLog, name: str) -> None:
    for row in MANIFEST["files"][name]["decision_rows"]:
        await decisions.append(
            Decision(
                ts_ms=row["ts_ms"],
                session_id=SessionId(row["session_id"]),
                source=row["source"],
                kind=row["kind"],
                source_file=row["source_file"],
                event=row["event"],
                action=row["action"],
                message=row["message"],
                tool_digest=ToolDigest(row["tool_digest"]) if row["tool_digest"] else None,
            )
        )


def nudge_attachment(content: str, *, tool_use_id: str = "t1", **overrides: Any) -> dict[str, Any]:
    return envelope(
        "attachment",
        attachment={
            "type": "hook_additional_context",
            "content": [content],
            "hookName": "PreToolUse:Bash",
            "toolUseID": tool_use_id,
            "hookEvent": "PreToolUse",
        },
        **overrides,
    )


def stop_feedback_turn(content: str, **overrides: Any) -> dict[str, Any]:
    return envelope(
        "user", message={"role": "user", "content": f"Stop hook feedback:\n{content}"}, isMeta=True, **overrides
    )


def complaint_entries(complaint: str, *, session: str = "sess-1") -> list[dict[str, Any]]:
    return [
        user_text("run a status check, then commit", sessionId=session),
        assistant_tool_use("t1", "Bash", {"command": "git status"}, sessionId=session),
        nudge_attachment(NUDGE_MESSAGE, sessionId=session),
        tool_result("t1", "clean", sessionId=session),
        assistant_text(complaint, sessionId=session),
    ]


@pytest_asyncio.fixture
async def decisions() -> AsyncIterator[DecisionLog]:
    async with await open_decision_log(decisions_db_path()) as log:
        yield log


async def seed_decision(
    decisions: DecisionLog,
    *,
    ts_ms: int = BASE_MS - 10_000,
    session_id: str = "sess-1",
    kind: str = "status_nudge:nudge_c424798f",
    source_file: str = PRIMITIVE_NUDGE,
    event: str = "PreToolUse",
    action: str = "warn",
    message: str | None = NUDGE_MESSAGE,
    tool_digest: ToolDigest | None = GIT_STATUS_DIGEST,
) -> None:
    await decisions.append(
        Decision(
            ts_ms=ts_ms,
            session_id=SessionId(session_id),
            source="captain-hook",
            kind=kind,
            source_file=source_file,
            event=event,
            action=action,
            message=message,
            tool_digest=tool_digest,
        )
    )


async def rows(store: ReviewStore, query: str) -> list[dict[str, Any]]:
    return await store.db.sql(query)


class TestMarkers:
    @pytest.mark.parametrize(
        ("text", "strength", "misfire_class"),
        [
            pytest.param("the hook re-fired on my own earlier text - ignoring it", "strong", "refire", id="refire"),
            pytest.param("this nudge is a false positive", "strong", "false_positive", id="false-positive"),
            pytest.param(
                "that stop gate shouldn't have fired - I had already addressed every open task",
                "strong",
                "already_addressed",
                id="already-addressed",
            ),
            pytest.param(
                "that stop gate shouldn't have fired at all here",
                "strong",
                "should_not_have_fired",
                id="should-not-have-fired",
            ),
            pytest.param("this reminder is spurious; the tests already pass", "strong", "spurious", id="spurious"),
            pytest.param(
                "noted, but this reminder re-fired on text I already fixed - ignoring it",
                "strong",
                "refire",
                id="strong-dismissal-overrides-compliance",
            ),
            pytest.param(
                "the hook fired incorrectly on text I never wrote",
                "strong",
                "incorrect_fire",
                id="verb-anchored-fired-incorrectly",
            ),
            pytest.param(
                "the hook incorrectly fired here", "strong", "incorrect_fire", id="verb-anchored-incorrectly-fired"
            ),
            pytest.param(
                "that nudge mistakenly triggered on a benign edit",
                "strong",
                "incorrect_fire",
                id="verb-anchored-mistakenly-triggered",
            ),
            pytest.param(
                "the gate erroneously flagged a clean file", "strong", "incorrect_fire", id="verb-anchored-erroneously"
            ),
            pytest.param(
                "I think the hook may be a false positive here", "hedged", "false_positive", id="hedged-known-class"
            ),
            pytest.param("the gate seems to have misfired", "hedged", "misfire", id="hedged-misfire"),
            pytest.param(
                "I think the reminder fired incorrectly here",
                "hedged",
                "incorrect_fire",
                id="hedged-verb-anchored-downgrades",
            ),
            pytest.param("the nudge seems incorrect here", "hedged", "suspected", id="hedged-adjective-only"),
            pytest.param("that warning was a false alarm", "strong", "false_alarm", id="warning-vocab-false-alarm"),
            pytest.param("the hook was a false alarm", "strong", "false_alarm", id="hook-false-alarm"),
            pytest.param(
                "that warning shouldn't have fired",
                "strong",
                "should_not_have_fired",
                id="warning-vocab-should-not-have-fired",
            ),
            pytest.param(
                "I think that warning may be a false alarm",
                "hedged",
                "false_alarm",
                id="hedged-false-alarm",
            ),
        ],
    )
    def test_marker_classification(self, text: str, strength: str, misfire_class: str) -> None:
        marker = classify_marker(text)
        assert marker is not None
        assert (marker.strength, marker.misfire_class) == (strength, misfire_class)

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("I'll fix the hook config now", id="compliance-ill"),
            pytest.param("good catch - the reminder is right, running the tests now", id="compliance-good-catch"),
            pytest.param("let me re-run the linter before the hook complains again", id="compliance-let-me"),
            pytest.param("noted - the gate wants a summary line", id="compliance-noted"),
            pytest.param("the user pointed out a false positive in my analysis", id="no-hook-vocabulary"),
            pytest.param("the policy hook blocked the force-delete, so I removed files individually", id="ambient"),
            pytest.param("the hook reminder about the task tracker has fired again", id="fired-again-not-refired"),
            pytest.param("I corrected the incorrect path the hook flagged", id="incorrect-adjective-no-hedge"),
            pytest.param("mistakenly deleted the file, then the hook ran", id="adverb-far-from-fire-verb"),
            pytest.param("the feature flag was removed", id="feature-flag-no-vocabulary"),
            pytest.param("CI flagged three new warnings", id="warnings-noun-no-marker"),
            pytest.param("fixed the compiler warning before committing", id="compiler-warning-no-marker"),
            pytest.param("I'll silence the warning the hook pointed at", id="warning-compliance"),
        ],
    )
    def test_non_complaints_yield_no_marker(self, text: str) -> None:
        assert classify_marker(text) is None


class TestFingerprints:
    @pytest.mark.parametrize("name", sorted(MANIFEST["files"]))
    def test_manifest_fingerprints_match_parser_view(self, name: str) -> None:
        events = fixture_events(name)
        for fp in MANIFEST["files"][name]["fingerprints"]:
            extracted = fingerprint_of(events[fp["line"] - 1])
            match fp["kind"]:
                case "hook_additional_context":
                    assert extracted is not None
                    assert extracted.message == "\n".join(fp["content"])
                    assert extracted.tool_use_id is not None
                case "hook_blocking_error":
                    assert extracted is not None
                    assert extracted.message == fp["blockingError"]["blockingError"]
                    assert extracted.event == "Stop"
                case "stop_hook_feedback_meta_turn":
                    assert extracted is not None
                    assert extracted.message == fp["content"].removeprefix("Stop hook feedback:\n")
                    assert extracted.event == "Stop"
                case "is_error_tool_result":
                    assert extracted is not None
                    assert extracted.message == fp["content"]
                    assert extracted.tool_use_id is not None
                case "hook_success":
                    assert extracted is None
                case unknown:
                    raise AssertionError(unknown)

    def test_ordinary_events_carry_no_fingerprint(self) -> None:
        events = parse_events_from_bytes(
            (
                json.dumps(user_text("hello"))
                + "\n"
                + json.dumps(assistant_text("hi"))
                + "\n"
                + json.dumps(tool_result("t1", "ok"))
            ).encode()
        )
        assert [fingerprint_of(event) for event in events] == [None, None, None]


def make_decision(*, kind: str, source_file: str) -> Decision:
    return Decision(
        ts_ms=BASE_MS,
        session_id=SessionId("sess-1"),
        source="captain-hook",
        kind=kind,
        source_file=source_file,
        event="PreToolUse",
        action="warn",
        message="m",
    )


class TestResolveTarget:
    @pytest.mark.parametrize(
        ("source_file", "kind", "expected"),
        [
            pytest.param(
                PRIMITIVE_NUDGE,
                "status_nudge:nudge_c424798f",
                Target(".claude/hooks/status_nudge.py", "status_nudge:nudge_c424798f", None, None),
                id="primitive-resolves-from-module-stem",
            ),
            pytest.param(
                PRIMITIVE_NUDGE,
                "hooks.tasks:nudge_9cf8ea99",
                Target(".claude/hooks/tasks.py", "hooks.tasks:nudge_9cf8ea99", None, None),
                id="primitive-strips-package-prefix",
            ),
            pytest.param(PRIMITIVE_NUDGE, "declarative_1", None, id="primitive-anonymous-unresolvable"),
            pytest.param(
                PRIMITIVE_NUDGE,
                "general.docs:nudge_1a2b3c4d",
                Target(
                    "captain_hook/packs/general/docs.py", "general.docs:nudge_1a2b3c4d", CAPTAIN_HOOK_REPO, "general"
                ),
                id="builtin-pack-resolves-into-captain-hook",
            ),
            pytest.param(
                "/x/site-packages/captain_hook/packs/general/docs.py",
                "general.docs:hook_1a2b3c4d",
                Target(
                    "captain_hook/packs/general/docs.py", "general.docs:hook_1a2b3c4d", CAPTAIN_HOOK_REPO, "general"
                ),
                id="wheel-pack-hook-resolves-into-captain-hook",
            ),
            pytest.param(
                NOTIFY_SOURCE,
                NOTIFY_KIND,
                Target(".claude/hooks/alerts.py", NOTIFY_KIND, None, None),
                id="undeclared-external-pack-cache-hook-falls-back-local",
            ),
            pytest.param(
                PRIMITIVE_NUDGE,
                "python.my_thing:nudge_1a2b3c4d",
                Target(".claude/hooks/my_thing.py", "python.my_thing:nudge_1a2b3c4d", None, None),
                id="packaged-user-hook-shadowing-builtin-pack-name-stays-local",
            ),
            pytest.param(
                PRIMITIVE_NUDGE, "<frozen importlib:nudge_a1b2c3d4", None, id="legacy-frozen-importlib-unresolvable"
            ),
            pytest.param(
                "/repo/.claude/hooks/guard.py",
                "guard:warn_deadbeef",
                Target("/repo/.claude/hooks/guard.py", "guard:warn_deadbeef", None, None),
                id="user-file-passes-through",
            ),
        ],
    )
    def test_resolve_target(self, source_file: str, kind: str, expected: Target | None) -> None:
        assert resolve_target(make_decision(kind=kind, source_file=source_file), INDEX) == expected

    def test_declared_external_pack_routes_to_its_repo(self) -> None:
        index = PackIndex(builtins=INDEX.builtins, externals={"notify": ExternalRoute("notify", NOTIFY_REPO)})
        target = resolve_target(make_decision(kind=NOTIFY_KIND, source_file=NOTIFY_SOURCE), index)
        assert target == Target("hooks/alerts.py", NOTIFY_KIND, NOTIFY_REPO, "notify")

    def test_hyphenated_external_pack_routes_by_sanitized_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pack named `ccx-rel` loads under the module prefix `ccx_rel`, so a firing decision's
        # kind carries `ccx_rel.<mod>:...`. The index must key by that sanitized runtime prefix — not
        # the canonical hyphenated name — or resolve_target misses and falls back to `.claude/hooks`.
        cache_external_pack(tmp_path, monkeypatch, name="ccx-rel")
        root = write_external_packs_toml(tmp_path / "proj", name="ccx-rel", source="github:acme/ccx-rel@v1")
        index = PackIndex.load(root)
        assert index.externals == {"ccx_rel": ExternalRoute("ccx-rel", RepoKey("github.com/acme/ccx-rel"))}
        source = "/home/u/.cache/captain-hook/packs/ccx-rel@abc1234/hooks/alerts.py"
        target = resolve_target(make_decision(kind="ccx_rel.alerts:hook_deadbeef", source_file=source), index)
        assert target == Target(
            "hooks/alerts.py", "ccx_rel.alerts:hook_deadbeef", RepoKey("github.com/acme/ccx-rel"), "ccx-rel"
        )

    def test_plugin_pack_finding_returns_none(self) -> None:
        # A discovered plugin pack has no repo target this wave: a misfire attributed to one drops
        # rather than misfiling its fix against the plugin's absolute install path or `.claude/hooks`.
        index = PackIndex(builtins=INDEX.builtins, externals={}, plugins=frozenset({"notify"}))
        source = "/home/u/.claude/plugins/cache/acme/notify/1.0.0/alerts.py"
        assert resolve_target(make_decision(kind="notify.alerts:hook_deadbeef", source_file=source), index) is None

    def test_repo_hook_sharing_a_plugin_name_stays_local(self) -> None:
        # A single-segment repo hook (`guard:...`) that merely shares a plugin pack's name is not a
        # plugin pack (those are always `<pack>.<mod>`), so it keeps its repo-local route.
        index = PackIndex(builtins=INDEX.builtins, externals={}, plugins=frozenset({"guard"}))
        source = "/repo/.claude/hooks/guard.py"
        assert resolve_target(make_decision(kind="guard:warn_deadbeef", source_file=source), index) == Target(
            source, "guard:warn_deadbeef", None, None
        )

    def test_bare_named_plugin_pack_hook_drops_by_source_dir(self) -> None:
        # A bare `@on` name carries no `<pack>.` prefix for the name arm, so the source-dir arm drops it.
        hooks_dir = "/home/u/.claude/plugins/cache/acme/notify/1.0.0/hooks"
        index = PackIndex(
            builtins=INDEX.builtins, externals={}, plugins=frozenset(), plugin_dirs=frozenset({hooks_dir})
        )
        assert resolve_target(make_decision(kind="my_bare_hook", source_file=f"{hooks_dir}/alerts.py"), index) is None

    def test_repo_hook_outside_plugin_dirs_stays_local(self) -> None:
        # The source arm matches only by containment: a repo hook outside every plugin dir stays local.
        index = PackIndex(
            builtins=INDEX.builtins,
            externals={},
            plugins=frozenset(),
            plugin_dirs=frozenset({"/home/u/.claude/plugins/cache/acme/notify/1.0.0/hooks"}),
        )
        source = "/repo/.claude/hooks/guard.py"
        assert resolve_target(make_decision(kind="guard:warn_deadbeef", source_file=source), index) == Target(
            source, "guard:warn_deadbeef", None, None
        )

    @pytest.mark.parametrize(
        ("source_file", "expected"),
        [
            pytest.param(NOTIFY_SOURCE, "hooks/alerts.py", id="nested-hooks-dir"),
            pytest.param("/c/.cache/captain-hook/packs/notify@sha/alerts.py", "alerts.py", id="hooks-beside-manifest"),
            pytest.param(PRIMITIVE_NUDGE, None, id="not-a-pack-cache-path"),
        ],
    )
    def test_external_target_path(self, source_file: str, expected: str | None) -> None:
        assert external_target_path(source_file) == expected


class TestPackIndex:
    def test_load_maps_builtin_packs_to_captain_hook(self) -> None:
        index = PackIndex.load(None)
        assert "general" in index.builtins
        assert index.externals == {}
        target = resolve_target(make_decision(kind="general.docs:hook_1", source_file=PRIMITIVE_NUDGE), index)
        assert target is not None
        assert target.repo == CAPTAIN_HOOK_REPO

    def test_load_maps_a_cached_external_pack_to_its_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_external_pack(tmp_path, monkeypatch)
        root = write_external_packs_toml(tmp_path / "proj")
        index = PackIndex.load(root)
        assert index.externals == {"notify": ExternalRoute("notify", NOTIFY_REPO)}

    def test_load_drops_a_declared_external_pack_that_is_not_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
        root = write_external_packs_toml(tmp_path / "proj")
        assert PackIndex.load(root).externals == {}

    def test_load_maps_a_discovered_plugin_pack_by_its_module_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "proj"
        plant_plugin_pack(tmp_path, monkeypatch, root, name="ccx-rel")
        assert PackIndex.load(root).plugins == frozenset({"ccx_rel"})

    def test_load_records_plugin_dirs_for_the_source_arm(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The source arm needs each non-shadowed plugin pack's hooks dir; PackIndex.load records them
        # (hooks="." here, so the pack root is the hooks dir).
        root = tmp_path / "proj"
        plugin_dir = plant_plugin_pack(tmp_path, monkeypatch, root, name="notify")
        assert PackIndex.load(root).plugin_dirs == frozenset({str(plugin_dir)})

    def test_load_reads_the_snapshot_without_a_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Discovery must never spawn `claude plugin list` on the scan path; PackIndex.load reads the
        # snapshot as-is, so stubbing the CLI boundary to explode proves it is never reached.
        def boom(_root: Path) -> tuple[plugins.EnabledPlugin, ...]:
            raise AssertionError("PackIndex.load must not run the plugin-list subprocess")

        monkeypatch.setattr(plugins, "list_plugins_cli", boom)
        root = tmp_path / "proj"
        plant_plugin_pack(tmp_path, monkeypatch, root)
        assert PackIndex.load(root).plugins == frozenset({"notify"})

    def test_load_lets_a_declared_entry_shadow_a_discovered_plugin_pack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "proj"
        plant_plugin_pack(tmp_path, monkeypatch, root)
        config = manager.config_path(root)
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[packs.notify]\ndisabled = true\n")
        assert PackIndex.load(root).plugins == frozenset()

    def test_load_has_no_plugins_without_a_snapshot(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(manager, "packs_cache_root", lambda: tmp_path / "cache")
        assert PackIndex.load(tmp_path / "proj").plugins == frozenset()

    def test_load_none_has_no_plugins(self) -> None:
        assert PackIndex.load(None).plugins == frozenset()


class TestNamedHookTarget:
    async def test_unique_kind_match_attributes(self, decisions: DecisionLog) -> None:
        await seed_decision(decisions, kind="task_tracking:nudge_abc12345")
        fire = await named_hook_target(TASK_TRACKING_COMPLAINT, decisions, SessionId("sess-1"), BASE_MS)
        assert fire is not None
        assert fire.kind == "task_tracking:nudge_abc12345"

    async def test_same_kind_twice_returns_nearest(self, decisions: DecisionLog) -> None:
        await seed_decision(decisions, kind="task_tracking:nudge_abc12345", ts_ms=BASE_MS - 120_000)
        await seed_decision(decisions, kind="task_tracking:nudge_abc12345", ts_ms=BASE_MS - 4_000)
        fire = await named_hook_target(TASK_TRACKING_COMPLAINT, decisions, SessionId("sess-1"), BASE_MS)
        assert fire is not None
        assert fire.ts_ms == BASE_MS - 4_000

    async def test_two_kinds_sharing_a_stem_fail_closed(self, decisions: DecisionLog) -> None:
        await seed_decision(decisions, kind="task_tracking:nudge_aaaa1111", ts_ms=BASE_MS - 8_000)
        await seed_decision(decisions, kind="task_tracking:gate_bbbb2222", ts_ms=BASE_MS - 6_000)
        assert await named_hook_target(TASK_TRACKING_COMPLAINT, decisions, SessionId("sess-1"), BASE_MS) is None

    async def test_two_named_hooks_both_fired_fail_closed(self, decisions: DecisionLog) -> None:
        await seed_decision(decisions, kind="task_tracking:nudge_abc12345", ts_ms=BASE_MS - 8_000)
        await seed_decision(decisions, kind="lint_guard:nudge_def67890", ts_ms=BASE_MS - 6_000)
        text = "the task-tracking hook and the lint-guard hook both misfired on me"
        assert await named_hook_target(text, decisions, SessionId("sess-1"), BASE_MS) is None

    async def test_no_match_outside_the_window(self, decisions: DecisionLog) -> None:
        await seed_decision(decisions, kind="task_tracking:nudge_abc12345", ts_ms=BASE_MS - 3_600_000)
        assert await named_hook_target(TASK_TRACKING_COMPLAINT, decisions, SessionId("sess-1"), BASE_MS) is None

    async def test_no_match_when_named_hook_never_fired(self, decisions: DecisionLog) -> None:
        await seed_decision(decisions, kind="lint_guard:nudge_abc12345")
        assert await named_hook_target(TASK_TRACKING_COMPLAINT, decisions, SessionId("sess-1"), BASE_MS) is None

    async def test_no_match_when_complaint_names_no_hook(self, decisions: DecisionLog) -> None:
        await seed_decision(decisions, kind="task_tracking:nudge_abc12345")
        text = "the task tracker reminder misfired again on a done task"
        assert await named_hook_target(text, decisions, SessionId("sess-1"), BASE_MS) is None

    async def test_fileless_decision_is_excluded(self, decisions: DecisionLog) -> None:
        await seed_decision(decisions, kind="task_tracking:nudge_abc12345", source_file="")
        assert await named_hook_target(TASK_TRACKING_COMPLAINT, decisions, SessionId("sess-1"), BASE_MS) is None


class TestDetector:
    async def test_real_misfire_complaint_attributes_to_user_hook_not_primitive(self, decisions: DecisionLog) -> None:
        await seed_fixture_decisions(decisions, MISFIRE_FIXTURE)
        [sig] = [s async for s in iter_hook_complaint_signals(fixture_events(MISFIRE_FIXTURE), decisions=decisions, index=INDEX)]
        assert sig.kind == HOOK_COMPLAINT
        assert "re-fired unnecessarily and I am ignoring the repeats" in sig.text
        assert sig.evidence["target_source_file"] == ".claude/hooks/status_nudge.py"
        assert sig.evidence["target_hook_name"] == "status_nudge:nudge_c424798f"
        assert str(sig.evidence["source_file"]).endswith("captain_hook/primitives/nudge.py")
        assert (sig.evidence["event"], sig.evidence["action"]) == ("PreToolUse", "warn")
        assert sig.evidence["fire_message"] == NUDGE_MESSAGE
        assert sig.evidence["fire_ts_ms"] == 1781224517348
        assert (sig.evidence["marker"], sig.evidence["misfire_class"]) == ("re-fired", "refire")
        assert sig.signal is not None
        assert sig.signal.confidence == VERY_HIGH
        assert sig.signal.reasons == ("strong_marker", "refire")

    @pytest.mark.parametrize("name", ["fire-compliance.jsonl", "fire-block.jsonl", "fire-stop.jsonl"])
    async def test_compliance_and_working_fire_fixtures_yield_nothing(self, decisions: DecisionLog, name: str) -> None:
        await seed_fixture_decisions(decisions, name)
        assert [s async for s in iter_hook_complaint_signals(fixture_events(name), decisions=decisions, index=INDEX)] == []

    async def test_complaint_with_no_preceding_fingerprint_yields_nothing(
        self, decisions: DecisionLog, tmp_path: Path
    ) -> None:
        path = tmp_path / "s.jsonl"
        entries = [
            user_text("run a status check"),
            assistant_text("checking now"),
            assistant_text(STRONG_COMPLAINT),
        ]
        write_transcript(path, entries)
        await seed_decision(decisions)
        events = parse_events_from_bytes(path.read_bytes())
        assert [s async for s in iter_hook_complaint_signals(events, decisions=decisions, index=INDEX)] == []

    async def test_named_hook_fallback_attributes_when_no_fingerprint_lands(
        self, decisions: DecisionLog, tmp_path: Path
    ) -> None:
        path = tmp_path / "s.jsonl"
        entries = [
            user_text("update the task tracker and keep working"),
            assistant_text("marking the current task done"),
            assistant_text("continuing with the next change"),
            assistant_text(TASK_TRACKING_COMPLAINT),
        ]
        write_transcript(path, entries)
        await seed_decision(decisions, kind="task_tracking:nudge_abc12345")
        events = parse_events_from_bytes(path.read_bytes())
        [sig] = [s async for s in iter_hook_complaint_signals(events, decisions=decisions, index=INDEX)]
        assert sig.evidence["attribution"] == "hook_name"
        assert sig.evidence["target_source_file"] == ".claude/hooks/task_tracking.py"
        assert sig.evidence["target_hook_name"] == "task_tracking:nudge_abc12345"
        assert (sig.evidence["marker"], sig.evidence["misfire_class"]) == ("misfire", "misfire")
        assert sig.trigger_index is None
        assert sig.signal is not None
        assert sig.signal.confidence == VERY_HIGH
        assert sig.signal.reasons == ("strong_marker", "misfire")

    async def test_named_hook_fallback_stays_closed_when_no_hook_is_named(
        self, decisions: DecisionLog, tmp_path: Path
    ) -> None:
        path = tmp_path / "s.jsonl"
        entries = [
            user_text("update the task tracker and keep working"),
            assistant_text("marking the current task done"),
            assistant_text(STRONG_COMPLAINT),
        ]
        write_transcript(path, entries)
        await seed_decision(decisions, kind="task_tracking:nudge_abc12345")
        events = parse_events_from_bytes(path.read_bytes())
        assert [s async for s in iter_hook_complaint_signals(events, decisions=decisions, index=INDEX)] == []

    async def test_complaint_with_no_decision_row_yields_nothing(self, decisions: DecisionLog) -> None:
        events = fixture_events(MISFIRE_FIXTURE)
        assert [s async for s in iter_hook_complaint_signals(events, decisions=decisions, index=INDEX)] == []

    async def test_fingerprints_attributing_to_two_targets_drop(self, decisions: DecisionLog, tmp_path: Path) -> None:
        other_message = "Prefer `eza` over `ls` in this repo."
        path = tmp_path / "s.jsonl"
        entries = [
            user_text("run the checks"),
            assistant_tool_use("t1", "Bash", {"command": "git status"}),
            nudge_attachment(NUDGE_MESSAGE, tool_use_id="t1"),
            assistant_tool_use("t2", "Bash", {"command": "ls"}),
            nudge_attachment(other_message, tool_use_id="t2"),
            assistant_text(STRONG_COMPLAINT),
        ]
        write_transcript(path, entries)
        await seed_decision(decisions)
        await seed_decision(
            decisions,
            kind="other_hook:nudge_deadbeef",
            source_file="/repo/.claude/hooks/other.py",
            message=other_message,
            tool_digest=tool_digest("Bash", {"command": "ls"}),
        )
        events = parse_events_from_bytes(path.read_bytes())
        assert [s async for s in iter_hook_complaint_signals(events, decisions=decisions, index=INDEX)] == []

    async def test_anonymous_declarative_fire_drops(self, decisions: DecisionLog, tmp_path: Path) -> None:
        deny = "BLOCKED: recursive force-delete (rm -rf) is forbidden in this repo."
        path = tmp_path / "s.jsonl"
        entries = [
            assistant_tool_use("t1", "Bash", {"command": "rm -rf scratch"}),
            tool_result("t1", deny, is_error=True),
            assistant_text(f"That rm guard misfired - the path is a scratch dir. {deny}"),
        ]
        write_transcript(path, entries)
        await seed_decision(
            decisions,
            kind="declarative_1",
            source_file="",
            action="block",
            message=deny,
            tool_digest=tool_digest("Bash", {"command": "rm -rf scratch"}),
        )
        events = parse_events_from_bytes(path.read_bytes())
        assert [s async for s in iter_hook_complaint_signals(events, decisions=decisions, index=INDEX)] == []

    async def test_tight_proximity_bumps_hedged_to_high(self, decisions: DecisionLog, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        entries = [
            user_text("run a status check"),
            assistant_tool_use("t1", "Bash", {"command": "git status"}),
            nudge_attachment(NUDGE_MESSAGE),
            assistant_text(HEDGED_COMPLAINT),
        ]
        write_transcript(path, entries)
        await seed_decision(decisions)
        events = parse_events_from_bytes(path.read_bytes())
        [sig] = [s async for s in iter_hook_complaint_signals(events, decisions=decisions, index=INDEX)]
        assert sig.signal is not None
        assert sig.signal.confidence == MEDIUM + 0.25
        assert sig.signal.reasons == ("hedged_marker", "misfire", "tight_proximity")

    async def test_digestless_stop_complaint_attributes_via_nearest(self, decisions: DecisionLog, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        entries = [
            user_text("wrap up the change"),
            assistant_text("done with the work"),
            stop_feedback_turn(STOP_MESSAGE),
            assistant_text(STOP_COMPLAINT),
        ]
        write_transcript(path, entries)
        await seed_decision(
            decisions,
            kind="stop_reminder:gate_e76ccd07",
            event="Stop",
            action="block",
            message=STOP_MESSAGE,
            tool_digest=None,
        )
        events = parse_events_from_bytes(path.read_bytes())
        [sig] = [s async for s in iter_hook_complaint_signals(events, decisions=decisions, index=INDEX)]
        assert sig.evidence["target_source_file"] == ".claude/hooks/stop_reminder.py"
        assert sig.evidence["target_hook_name"] == "stop_reminder:gate_e76ccd07"
        assert (sig.evidence["event"], sig.evidence["action"]) == ("Stop", "block")
        assert sig.evidence["misfire_class"] == "already_addressed"

    async def test_digestless_attribution_requires_the_message_tiebreak(self, decisions: DecisionLog, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        entries = [
            user_text("wrap up the change"),
            assistant_text("done with the work"),
            stop_feedback_turn(STOP_MESSAGE),
            assistant_text(STOP_COMPLAINT),
        ]
        write_transcript(path, entries)
        await seed_decision(
            decisions,
            kind="other_gate:gate_deadbeef",
            event="Stop",
            action="block",
            message="A different gate's text entirely.",
            tool_digest=None,
        )
        events = parse_events_from_bytes(path.read_bytes())
        assert [s async for s in iter_hook_complaint_signals(events, decisions=decisions, index=INDEX)] == []


class TestStrictFixPartition:
    def hedged_entries(self) -> list[dict[str, Any]]:
        return [
            user_text("run a status check"),
            nudge_attachment(NUDGE_MESSAGE),
            assistant_tool_use("t1", "Bash", {"command": "git status"}),
            tool_result("t1", "clean"),
            assistant_text(HEDGED_COMPLAINT),
        ]

    async def test_hedged_complaint_passes_the_default_fix_floor(
        self, store: ReviewStore, settings: ReviewSettings, decisions: DecisionLog, tmp_path: Path
    ) -> None:
        path = write_transcript(tmp_path / "s.jsonl", self.hedged_entries())
        await seed_decision(decisions)
        assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(scanned=1, inserted=1)
        [event] = await rows(store, "SELECT * FROM feedback_events")
        assert event["source_kind"] == HOOK_COMPLAINT
        assert json.loads(str(event["payload_json"]))["signal"]["confidence"] == MEDIUM

    async def test_hedged_complaint_drops_under_a_raised_fix_floor(
        self, store: ReviewStore, settings: ReviewSettings, decisions: DecisionLog, tmp_path: Path
    ) -> None:
        raised = ReviewSettings(db_path=settings.db_path, min_confidence_fix=HIGH)
        path = write_transcript(tmp_path / "s.jsonl", self.hedged_entries())
        await seed_decision(decisions)
        assert await scan_transcript(store, path, settings=raised, repo_key=REPO) == ScanReport(scanned=1, inserted=0)
        assert await rows(store, "SELECT * FROM feedback_events") == []
        assert await rows(store, "SELECT * FROM candidates") == []


class TestFixGroupingAndStore:
    async def test_end_to_end_fixture_scan_creates_fix_candidate_with_target(
        self, store: ReviewStore, settings: ReviewSettings, decisions: DecisionLog
    ) -> None:
        path = FIXTURES / MISFIRE_FIXTURE
        await seed_fixture_decisions(decisions, MISFIRE_FIXTURE)
        assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(scanned=1, inserted=1)

        [event] = await rows(store, "SELECT * FROM feedback_events")
        assert event["source_kind"] == HOOK_COMPLAINT
        payload = json.loads(str(event["payload_json"]))
        assert payload["target_source_file"] == ".claude/hooks/status_nudge.py"
        assert payload["signal"]["confidence"] == VERY_HIGH
        assert ContextWindow.from_json(str(event["context_json"])).anchor is not None

        [candidate] = await rows(store, "SELECT * FROM candidates")
        assert (candidate["candidate_kind"], candidate["status"]) == ("fix", "watching")
        assert candidate["target_source_file"] == ".claude/hooks/status_nudge.py"
        assert candidate["target_hook_name"] == "status_nudge:nudge_c424798f"
        assert candidate["misfire_class"] == "refire"
        assert candidate["rule"] == dedup_key(
            "hook_complaint", "status_nudge:nudge_c424798f", ".claude/hooks/status_nudge.py"
        )

        await store.enable(REPO)
        [observation] = await rows(store, "SELECT * FROM candidate_observations")
        await store.record_verdict(
            DedupKey(str(observation["dedup_key"])),
            Verdict(),
            role="judge",
            prompt_version=store.versions.fix,
            model="m1",
            fidelity="full",
        )
        status = await store.threshold_status(int(candidate["id"]), settings=settings)
        assert (status.sessions, status.single_observation) == (1, True)
        assert await store.eligible(int(candidate["id"]), settings=settings)

    async def test_two_sessions_complaints_about_one_hook_group_under_one_candidate(
        self, store: ReviewStore, settings: ReviewSettings, decisions: DecisionLog, tmp_path: Path
    ) -> None:
        for session in ("s1", "s2"):
            path = write_transcript(tmp_path / f"{session}.jsonl", complaint_entries(STRONG_COMPLAINT, session=session))
            await seed_decision(decisions, session_id=session)
            assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(
                scanned=1, inserted=1
            )

        [candidate] = await rows(store, "SELECT * FROM candidates")
        assert candidate["candidate_kind"] == "fix"
        observations = await rows(store, "SELECT * FROM candidate_observations")
        assert {row["candidate_id"] for row in observations} == {candidate["id"]}
        assert {row["session_id"] for row in observations} == {"s1", "s2"}

        await store.enable(REPO)
        for observation in observations:
            await store.record_verdict(
                DedupKey(str(observation["dedup_key"])),
                Verdict(),
                role="judge",
                prompt_version=store.versions.fix,
                model="m1",
                fidelity="full",
            )
        status = await store.threshold_status(int(candidate["id"]), settings=settings)
        assert status.sessions == 2
        assert await store.eligible(int(candidate["id"]), settings=settings)


class TestPackTargetRouting:
    async def test_builtin_pack_complaint_routes_to_captain_hook_with_origin_provenance(
        self, store: ReviewStore, settings: ReviewSettings, decisions: DecisionLog, tmp_path: Path
    ) -> None:
        path = write_transcript(tmp_path / "s.jsonl", complaint_entries(STRONG_COMPLAINT))
        await seed_decision(decisions, kind="general.docs:nudge_1a2b3c4d")
        assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(scanned=1, inserted=1)
        [candidate] = await rows(store, "SELECT * FROM candidates")
        assert candidate["repo_key"] == "github.com/yasyf/captain-hook"
        assert candidate["origin_repo_key"] == REPO
        assert candidate["pack_name"] == "general"
        assert candidate["target_source_file"] == "captain_hook/packs/general/docs.py"
        assert candidate["target_hook_name"] == "general.docs:nudge_1a2b3c4d"

    async def test_cached_external_pack_complaint_routes_to_the_pack_repo(
        self,
        store: ReviewStore,
        settings: ReviewSettings,
        decisions: DecisionLog,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache_external_pack(tmp_path, monkeypatch)
        root = write_external_packs_toml(tmp_path / "proj")
        entries = [
            user_text("run a status check, then commit", cwd=str(root)),
            assistant_tool_use("t1", "Bash", {"command": "git status"}, cwd=str(root)),
            nudge_attachment(NUDGE_MESSAGE, cwd=str(root)),
            tool_result("t1", "clean", cwd=str(root)),
            assistant_text(STRONG_COMPLAINT, cwd=str(root)),
        ]
        path = write_transcript(tmp_path / "s.jsonl", entries)
        await seed_decision(decisions, kind=NOTIFY_KIND, source_file=NOTIFY_SOURCE)
        assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(scanned=1, inserted=1)
        [candidate] = await rows(store, "SELECT * FROM candidates")
        assert candidate["repo_key"] == NOTIFY_REPO
        assert candidate["origin_repo_key"] == REPO
        assert candidate["pack_name"] == "notify"
        assert candidate["target_source_file"] == "hooks/alerts.py"
        assert candidate["target_hook_name"] == NOTIFY_KIND

    async def test_hooks_targeted_complaint_keeps_the_session_repo(
        self, store: ReviewStore, settings: ReviewSettings, decisions: DecisionLog, tmp_path: Path
    ) -> None:
        path = write_transcript(tmp_path / "s.jsonl", complaint_entries(STRONG_COMPLAINT))
        await seed_decision(decisions)
        assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(scanned=1, inserted=1)
        [candidate] = await rows(store, "SELECT * FROM candidates")
        assert candidate["repo_key"] == REPO
        assert candidate["origin_repo_key"] is None
        assert candidate["pack_name"] is None
        assert candidate["target_source_file"] == ".claude/hooks/status_nudge.py"


class TestFixJudge:
    @pytest.mark.parametrize(
        ("category", "accepted"),
        [
            pytest.param("misfire_confirmed", True, id="misfire-accepted"),
            pytest.param("compliance", False, id="compliance-rejected"),
            pytest.param("ambient_mention", False, id="ambient-rejected"),
        ],
    )
    def test_fix_categories_drive_acceptance(self, category: Category, accepted: bool) -> None:
        verdict = ReviewVerdict(category=category, summary="s", confidence=0.5, rationale="r")
        assert verdict.accepted is accepted

    async def test_hook_complaint_rows_get_the_fix_prompt(self) -> None:
        window = ContextWindow(
            anchor=EventRef(SessionId("s1"), EventUuid("u1")),
            before=(TurnRef(role="assistant", refs=(), preview="running git status", tool_digests=()),),
            trigger=None,
            after=(),
            fidelity="full",
            preview_chars=200,
        )
        row = {
            "source_kind": "hook_complaint",
            "context_json": window.to_json(),
            "text": STRONG_COMPLAINT,
            "payload_json": json.dumps(
                {
                    "target_hook_name": "status_nudge:nudge_c424798f",
                    "event": "PreToolUse",
                    "action": "warn",
                    "fire_message": NUDGE_MESSAGE,
                }
            ),
        }
        prompt, fidelity = await build_prompt(row)
        assert fidelity == "summary"
        assert SUMMARY_LABEL in prompt
        assert "misfire_confirmed" in prompt
        assert "[hook: status_nudge:nudge_c424798f (PreToolUse/warn)]" in prompt
        assert NUDGE_MESSAGE in prompt
        assert STRONG_COMPLAINT in prompt
        assert "running git status" in prompt

    async def test_create_rows_keep_the_create_prompt(self) -> None:
        window = ContextWindow(
            anchor=EventRef(SessionId("s2"), EventUuid("u2")),
            before=(),
            trigger=None,
            after=(),
            fidelity="full",
            preview_chars=200,
        )
        row = {"source_kind": "transcript_message", "context_json": window.to_json(), "text": "never do X"}
        prompt, fidelity = await build_prompt(row)
        assert fidelity == "summary"
        assert "DURABLE correction worth encoding as an" in prompt


class TestGoldenReview:
    def load(self) -> tuple[list[GoldenRow], str]:
        raw = (FIXTURES / "golden_review.json").read_bytes()
        golden = [
            GoldenRow(
                dedup_key=row["dedup_key"],
                source_kind=row["source_kind"],
                text=row["text"],
                expected=row["expected"] == "accepted",
                note=row["note"],
            )
            for row in json.loads(raw)
        ]
        return golden, hashlib.sha256(raw).hexdigest()

    def fake_judge(self, text: str) -> ReviewVerdict:
        if (marker := classify_marker(text)) is not None:
            return ReviewVerdict(
                category="misfire_confirmed",
                summary="claims the hook fired wrongly",
                confidence=0.9 if marker.strength == "strong" else 0.7,
                rationale=marker.misfire_class,
            )
        if COMPLIANCE_RE.search(text):
            return ReviewVerdict(category="compliance", summary="follows the hook", confidence=0.8, rationale="c")
        return ReviewVerdict(category="ambient_mention", summary="mentions a hook", confidence=0.8, rationale="a")

    def test_fixture_shape(self) -> None:
        golden, _ = self.load()
        assert len(golden) == 14
        assert sum(row.expected for row in golden) == 4
        assert {row.source_kind for row in golden} == {"hook_complaint"}
        assert len({row.dedup_key for row in golden}) == len(golden)

    def test_golden_gate_passes_against_the_fake_judge(self) -> None:
        golden, sha = self.load()
        verdicts = {
            row.dedup_key: {
                "accepted": (verdict := self.fake_judge(row.text)).accepted,
                "category": verdict.category,
                "rationale": verdict.rationale,
            }
            for row in golden
        }
        result = golden_result(golden, {row.dedup_key for row in golden}, verdicts, sha)
        assert result.failures == ()
        assert (result.passed, result.total, result.sha256) == (14, 14, sha)

    def test_golden_gate_flags_a_regressed_judge(self) -> None:
        golden, sha = self.load()
        verdicts = {
            row.dedup_key: {"accepted": True, "category": "misfire_confirmed", "rationale": "r"} for row in golden
        }
        result = golden_result(golden, {row.dedup_key for row in golden}, verdicts, sha)
        assert result.passed == 4
        assert {failure.expected for failure in result.failures} == {False}
