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
WIDGETS_DIR = BUILD_DIR / "tutorial" / "widgets"
MARKER = re.compile(r"<!-- gd-embed-widget: (\w+)( mode=canned)? -->")

LIVE_NOTE = "This runs a browser model of the demo subset — run `capt-hook test` for the real engine."
CANNED_NOTE = "Recorded from the real engine, not evaluated in your browser."


def widget_data(widget: dict) -> dict:
    if widget["mode"] == "canned":
        recordings = [{"id": c["id"], "input": c["input"], "verdict": c["verdict"]} for c in widget["cases"]]
        return {"mode": "canned", "hooks": [], "cases": [], "recordings": recordings}
    compiled = compile_fragment(FRAGMENTS_SRC / f"{widget['fragment']}.py")
    event = widget.get("event", "PreToolUse")
    cases = [
        {"event": event, **c.get("input", {}), **({"session": c["session"]} if "session" in c else {})}
        for c in widget["cases"]
    ]
    return {"mode": "live", "hooks": compiled["hooks"], "cases": cases, "recordings": []}


def widget_block(widget_id: str, matrix: dict) -> str:
    data = widget_data(matrix["widgets"][widget_id])
    note = CANNED_NOTE if data["mode"] == "canned" else LIVE_NOTE
    payload = json.dumps(data).replace("</", "<\\/")
    return (
        f'<div class="ch-widget" data-widget="{widget_id}" data-mode="{data["mode"]}">\n'
        f'<script type="application/json" class="ch-widget-data">{payload}</script>\n'
        f"</div>\n"
        f'<p class="ch-widget-note">{note}</p>'
    )


def expand(text: str, matrix: dict, qmd: Path) -> str:
    emitted = False

    def replace(match: re.Match[str]) -> str:
        nonlocal emitted
        parts = []
        if not emitted:
            emitted = True
            css = os.path.relpath(WIDGETS_DIR / "emulator.css", qmd.parent)
            js = os.path.relpath(WIDGETS_DIR / "emulator.js", qmd.parent)
            parts.append(f'<link rel="stylesheet" href="{css}">')
            parts.append(widget_block(match.group(1), matrix))
            parts.append(f'<script type="module" src="{js}"></script>')
        else:
            parts.append(widget_block(match.group(1), matrix))
        return "```{=html}\n" + "\n".join(parts) + "\n```"

    return MARKER.sub(replace, text)


def main() -> None:
    matrix = json.loads(MATRIX.read_text())
    for qmd in BUILD_DIR.rglob("*.qmd"):
        text = qmd.read_text()
        if (new := expand(text, matrix, qmd)) != text:
            qmd.write_text(new)


if __name__ == "__main__":
    main()
