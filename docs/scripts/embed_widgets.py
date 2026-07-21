from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent / "docs" / "scripts"))

from widget_compiler import compile_fragment  # noqa: E402

import captain_hook  # noqa: E402

# Expands "<!-- gd-embed-widget: id -->" markers into an emulator widget, mirroring
# embed_fragments.py's build-dir / source-tree split.
BUILD_DIR = Path(__file__).resolve().parents[1]
SOURCE = BUILD_DIR.parent / "docs"
FRAGMENTS_SRC = SOURCE / "_fragments"
MATRIX = SOURCE / "tutorial" / "_src" / "matrix.json"
WIDGETS_DIR = BUILD_DIR / "docs" / "tutorial" / "widgets"
PACKS = Path(captain_hook.__file__).parent / "builtin_packs"
MARKER = re.compile(r"<!-- gd-embed-widget: (\w+) -->")


def presentation_case(case: dict, event: str) -> dict:
    return {
        "event": event,
        **case.get("input", {}),
        **({"session": case["session"]} if "session" in case else {}),
        **({"label": case["label"]} if "label" in case else {}),
        **({"featured": case["featured"]} if "featured" in case else {}),
    }


def widget_data(widget: dict) -> dict:
    if widget["mode"] == "canned":
        recordings = [{"id": c["id"], "input": c["input"], "verdict": c["verdict"]} for c in widget["cases"]]
        return {"mode": "canned", "hooks": [], "cases": [], "recordings": recordings}
    if widget["mode"] == "world":
        event = widget.get("event", "PreToolUse")
        return {
            "mode": "world",
            "source": (PACKS / widget["pack"] / "hooks" / "deletions.py").read_text(),
            "world": widget["world"],
            "cases": [presentation_case(c, event) for c in widget["cases"]],
            "recordings": [],
        }
    fragment = FRAGMENTS_SRC / f"{widget['fragment']}.py"
    event = widget.get("event", "PreToolUse")
    return {
        "mode": "live",
        "source": fragment.read_text(),
        "hooks": compile_fragment(fragment)["hooks"],
        "cases": [presentation_case(c, event) for c in widget["cases"]],
        "recordings": [],
    }


def widget_block(widget_id: str, matrix: dict, qmd: Path) -> str:
    data = widget_data(matrix["widgets"][widget_id])
    payload = json.dumps(data).replace("</", "<\\/")
    editor_js = os.path.relpath(WIDGETS_DIR / "editor.js", qmd.parent)
    compiler_js = os.path.relpath(WIDGETS_DIR / "compiler.js", qmd.parent)
    return (
        f'<div class="ch-widget" data-widget="{widget_id}" data-mode="{data["mode"]}"'
        f' data-editor-js="{editor_js}" data-compiler-js="{compiler_js}">\n'
        f'<script type="application/json" class="ch-widget-data">{payload}</script>\n'
        f"</div>"
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
            parts.append(widget_block(match.group(1), matrix, qmd))
            parts.append(f'<script type="module" src="{js}"></script>')
        else:
            parts.append(widget_block(match.group(1), matrix, qmd))
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
