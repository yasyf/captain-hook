from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import run_cli


@pytest.mark.parametrize("transport", ["identity", "curl-data"])
def test_repo_hook_uses_the_installed_native_request_body_reader(tmp_path: Path, transport: str) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    head = "a" * 40
    cases = [
        (f'{{"bypass_rules":true,"sha":"{head}"}}', "Block()"),
        (f'{{"bypass_rules":true,"bypass_rules":false,"sha":"{head}"}}', "Allow()"),
        (f'{{"bypass_rules":1,"sha":"{head}"}}', "Allow()"),
        ('{"bypass_rules":tr\r\n\0ue,"sha":"' + head + '"}', "Block()" if transport == "curl-data" else "Allow()"),
        (f'{{"bypass_rules":true,"sha":"{head}"}} false', "Allow()"),
    ]
    fixtures = ",\n".join(
        f"Input(tool='Read', file=FileFixture(content={body!r})): {expected}"
        for body, expected in cases
    )
    (hooks / "request_body.py").write_text(f'''
from __future__ import annotations

from captain_hook import Allow, BaseHookEvent, Block, Event, FileFixture, Input, Tool, hook
from cc_transcript.parser import project_request_body

class NativeBypass:
    def check(self, evt: BaseHookEvent) -> bool:
        assert evt.file is not None
        body = project_request_body(evt.file.path, transport={transport!r})
        return body is not None and body["bypass_rules"] is True and body["sha"] == {head!r}

hook(
    Event.PreToolUse,
    block=True,
    only_if=[Tool("Read"), NativeBypass()],
    message="Native async bypass requires a pushed from-owner approval naming this PR and its current full head SHA.",
    tests={{
{fixtures}
    }},
)
''')
    result = run_cli("test", "--json", hooks_dir=str(hooks), root_dir=str(tmp_path), timeout=30)
    assert result.returncode == 0, (result.stdout, result.stderr)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(records) == len(cases)
    assert all(record["status"] == "pass" for record in records), records
