from __future__ import annotations

import re
from pathlib import Path

BUILD_DIR = Path(__file__).resolve().parents[1]
FRAGMENTS_SRC = BUILD_DIR.parent / "docs" / "_fragments"
MARKER = re.compile(r"<!-- gd-embed-fragment: (\w+) -->")

# Replaces "<!-- gd-embed-fragment: name -->" markers in the build-dir pages with
# the fenced source of docs/_fragments/name.py. Mirrors embed_examples.py so a
# canonical snippet (the hero hook) is authored once and injected everywhere it
# appears — the homepage and the quickstart can no longer drift apart.


def embed(match: re.Match[str]) -> str:
    source = (FRAGMENTS_SRC / f"{match.group(1)}.py").read_text().rstrip()
    return f"```python\n{source}\n```"


def main() -> None:
    for qmd in BUILD_DIR.rglob("*.qmd"):
        text = qmd.read_text()
        if (new := MARKER.sub(embed, text)) != text:
            qmd.write_text(new)


if __name__ == "__main__":
    main()
