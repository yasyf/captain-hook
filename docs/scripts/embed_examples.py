from __future__ import annotations

import re
from pathlib import Path

BUILD_DIR = Path(__file__).resolve().parents[1]

# Replaces "<!-- gd-embed: name.py -->" markers in the build-dir example pages
# with the fenced source of docs/examples/name.py. Runs as a Quarto pre-render
# script; a {{< include >}} shortcode cannot work here because Quarto expands
# includes while building the project context, before pre-render scripts run.


def embed(match: re.Match[str]) -> str:
    source = (BUILD_DIR.parent / "docs" / "examples" / f"{match.group(1)}.py").read_text().rstrip()
    return f"```python\n{source}\n```"


def main() -> None:
    for qmd in (BUILD_DIR / "docs" / "examples").glob("*.qmd"):
        text = qmd.read_text()
        if (new := re.sub(r"<!-- gd-embed: (\w+)\.py -->", embed, text)) != text:
            qmd.write_text(new)


if __name__ == "__main__":
    main()
