import asyncio
import copy
import json
import time

from captain_hook.snapshots.client import DEFAULT_LIMITS, HOST_SCHEMA
from captain_hook.snapshots.review import REVIEW_POLICY
from captain_hook.testing.snapshots import FixtureOwner
from tests.review_helpers import REPO, correction_entries, envelope


async def test_unused_large_tool_result_preserves_review_candidates_under_small_projection_budget(tmp_path):
    prefix = [
        envelope(
            "assistant",
            message={
                "role": "assistant",
                "model": "claude",
                "content": [
                    {"type": "tool_use", "id": "unused", "name": "Read", "input": {"file_path": "/repo/unrelated.txt"}}
                ],
            },
        ),
        envelope(
            "user",
            message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "unused", "content": ""}]},
        ),
    ]
    entries = prefix + correction_entries()
    fixture = FixtureOwner()
    candidates = []
    limits = DEFAULT_LIMITS | {"max_output_bytes": 32 * 1024}
    try:
        for size in (4, 256 * 1024):
            variant = copy.deepcopy(entries)
            variant[1]["message"]["content"][0]["content"] = "x" * size
            source = tmp_path / f"input-{size}.jsonl"
            source.write_text("".join(json.dumps(entry) + "\n" for entry in variant))
            context = fixture.context.copy()
            context["registry_generation"] = fixture.owner.store.register_tool_registry(
                fixture.client.tool_registry(), context=context
            )
            request = {
                "schema": HOST_SCHEMA,
                "id": f"projection-{size}",
                "operation": "prepare_review",
                "path": str(source),
                "deadline_unix_ms": int((time.time() + 30) * 1000),
                "policy": REVIEW_POLICY,
                "repo_key": REPO,
                "min_confidence": 0.5,
                "min_confidence_fix": 0.5,
                "limits": limits,
                "decision_log_path": str(tmp_path / "decisions.db"),
                "claude_config_dir": str(tmp_path),
            }

            def evaluate(snapshot):
                result = asyncio.run(fixture.owner.policy.prepare_review(snapshot, request))
                assert result["disposition"] == "eligible"
                assert snapshot.work["output_bytes"] < limits["max_output_bytes"]
                return result

            result = await asyncio.to_thread(
                fixture.owner.store.run_registered_review,
                source,
                **fixture.owner.review_inputs,
                request=request,
                context=context,
                cancellation=fixture.owner.token_type(),
                handler=evaluate,
            )
            decoded = [json.loads(value) for value in result["candidates_json"]]
            candidates.append([(row["dedup_key"], row["source_kind"], row["text"]) for row in decoded])
        assert candidates[0]
        assert candidates[0] == candidates[1]
    finally:
        fixture.close()
