from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from widget_compiler import compile_fragment  # noqa: E402

# Expands "<!-- gd-embed-widget: id -->" markers into an emulator widget, mirroring
# embed_fragments.py's build-dir / source-tree split.
BUILD_DIR = Path(__file__).resolve().parents[1]
SOURCE = BUILD_DIR.parent / "docs"
FRAGMENTS_SRC = SOURCE / "_fragments"
MATRIX = SOURCE / "tutorial" / "_src" / "matrix.json"
WIDGET_BUNDLE = BUILD_DIR / "tutorial" / "widgets" / "emulator.js"
MARKER = re.compile(r"<!-- gd-embed-widget: (\w+)( mode=canned)? -->")

LIVE_NOTE = "This runs a browser model of the demo subset — run `capt-hook test` for the real engine."
CANNED_NOTE = "Recorded from the real engine, not live."


def widget_data(widget: dict) -> dict:
    if widget["mode"] == "canned":
        recordings = [{"id": c["id"], "input": c["input"], "verdict": c["verdict"]} for c in widget["cases"]]
        return {"mode": "canned", "hooks": [], "cases": [], "recordings": recordings}
    compiled = compile_fragment(FRAGMENTS_SRC / f"{widget['fragment']}.py")
    event = widget.get("event", "PreToolUse")
    cases = [{"event": event, **c["input"]} for c in widget["cases"]]
    return {"mode": "live", "hooks": compiled["hooks"], "cases": cases, "recordings": []}


def widget_block(widget_id: str, matrix: dict) -> str:
    widget = matrix["widgets"][widget_id]
    data = widget_data(widget)
    note = CANNED_NOTE if data["mode"] == "canned" else LIVE_NOTE
    payload = json.dumps(data).replace("</", "<\\/")
    return (
        f'<div class="ch-emu" data-widget="{widget_id}" data-mode="{data["mode"]}">\n'
        f'<script type="application/json" class="ch-emu-data">{payload}</script>\n'
        f"</div>\n"
        f'<p class="ch-emu-note">{note}</p>'
    )


def expand(text: str, matrix: dict, qmd: Path) -> str:
    emitted = False

    def replace(match: re.Match[str]) -> str:
        nonlocal emitted
        block = widget_block(match.group(1), matrix)
        if emitted:
            return block
        emitted = True
        src = os.path.relpath(WIDGET_BUNDLE, qmd.parent)
        return f'{block}\n<script type="module" src="{src}"></script>'

    return MARKER.sub(replace, text)


def main() -> None:
    matrix = json.loads(MATRIX.read_text())
    for qmd in BUILD_DIR.rglob("*.qmd"):
        text = qmd.read_text()
        if (new := expand(text, matrix, qmd)) != text:
            qmd.write_text(new)


if __name__ == "__main__":
    main()
