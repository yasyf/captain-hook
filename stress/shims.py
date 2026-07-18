"""Executable shims installed into each sandbox's ``bin/``.

``capt-hook`` and ``uvx`` pin every invocation — including the wired SessionEnd
hook and the brain skill's hardcoded ``uvx capt-hook`` calls — to the local
checkout's venv. ``claude``, ``codex``, and ``gh`` are deterministic stand-ins
for the offline tier: the judge stub answers the structured-output protocol
spawnllm parses (a ``{"type": "result", "structured_output": ...}`` line), the
codex stub answers the ``review``-specialty correction-extraction call the
detached reviewer makes (``select_backend(specialty="review")`` routes to
``codex``) so ``--live none`` never reaches the real CLI, and the gh stub serves
canned PR states from ``STRESS_GH_STUB_CONFIG``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
VENV_BIN = CHECKOUT / ".venv" / "bin"

CAPT_HOOK_SHIM = f"""#!/bin/sh
exec "{VENV_BIN}/capt-hook" "$@"
"""

UVX_SHIM_TEMPLATE = """#!/bin/sh
if [ "$1" = "--isolated" ] && [ "$2" = "capt-hook" ]; then
  shift 2
  exec "{capt_hook}" "$@"
fi
if [ "$1" = "capt-hook" ]; then
  shift
  exec "{capt_hook}" "$@"
fi
exec "{real_uvx}" "$@"
"""

JUDGE_STUB = f"""#!{VENV_BIN}/python
import json, os, re, sys

DURABLE = {{"durable_style_rule", "workflow_rule", "tooling_rule", "safety_guard"}}

def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")

prompt = sys.stdin.read()
if log := os.environ.get("STRESS_JUDGE_STUB_LOG"):
    with open(log, "a") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "prompt_chars": len(prompt)}}) + "\\n")
mode = os.environ.get("STRESS_JUDGE_STUB", "marker")
if mode == "fail":
    sys.exit(1)
if mode == "garbage":
    print("not json at all")
    sys.exit(0)
marker = re.search(r"\\[\\[judge:([a-z_]+)(?::([0-9.]+))?(?::([a-z0-9-]+))?\\]\\]", prompt)
category = (marker.group(1) if marker else "ambient_noise") if mode == "marker" else mode
confidence = float(marker.group(2)) if mode == "marker" and marker and marker.group(2) else 0.9
feedback = prompt.split("=== FEEDBACK TO CLASSIFY ===\\n", 1)[-1]
rule_slug = (
    (marker.group(3) if marker and marker.group(3) else slugify(" ".join(feedback.split()[:4])))
    if category in DURABLE
    else None
)
verdict = {{"category": category, "summary": "stub verdict", "confidence": confidence, "rationale": "stub"}}
verdict["rule_slug"] = rule_slug
print(json.dumps({{"type": "result", "structured_output": verdict}}))
"""

CODEX_STUB = f"""#!{VENV_BIN}/python
import json, sys

args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    sys.exit(0)
sys.stdin.read()
if "-o" in args:
    with open(args[args.index("-o") + 1], "w") as fh:
        json.dump({{"candidate": None, "note": "offline stub: no faulted edit"}}, fh)
sys.exit(0)
"""

GH_STUB = f"""#!{VENV_BIN}/python
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
config = (
    json.loads(Path(path).read_text()) if (path := os.environ.get("STRESS_GH_STUB_CONFIG")) else {{}}
)
if log := os.environ.get("STRESS_GH_STUB_LOG"):
    with open(log, "a") as fh:
        fh.write(json.dumps(args) + "\\n")
if args[:2] == ["pr", "view"] and "--json" in args:
    state = config.get(args[2])
    if state in (None, "error"):
        print("stub: no such pr", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({{"state": state, "mergedAt": None}}))
    sys.exit(0)
sys.exit(1)
"""


def real_uvx() -> str:
    if (found := shutil.which("uvx")) is None:
        raise FileNotFoundError("uvx not on PATH")
    return found


def install(bin_dir: Path, name: str, content: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(content)
    path.chmod(0o755)
    return path


def install_base_shims(bin_dir: Path) -> None:
    capt_hook = install(bin_dir, "capt-hook", CAPT_HOOK_SHIM)
    install(bin_dir, "uvx", UVX_SHIM_TEMPLATE.format(capt_hook=capt_hook, real_uvx=real_uvx()))


def install_judge_stub(bin_dir: Path) -> None:
    install(bin_dir, "claude", JUDGE_STUB)


def install_codex_stub(bin_dir: Path) -> None:
    install(bin_dir, "codex", CODEX_STUB)


def install_gh_stub(bin_dir: Path) -> None:
    install(bin_dir, "gh", GH_STUB)
