from __future__ import annotations

import re
from pathlib import Path

BUILD_DIR = Path(__file__).resolve().parents[1]
SKILLS_SRC = BUILD_DIR.parent / "captain_hook" / "skills"
MARKER = re.compile(r"<!-- gd-embed-skill: ([a-z0-9-]+) -->")

# Replaces "<!-- gd-embed-skill: name -->" markers in the build-dir pages with
# the fenced source of captain_hook/skills/name/SKILL.md. Runs as a Quarto
# pre-render script, mirroring embed_examples.py; read_text() raising on a
# moved or renamed skill is what keeps the docs in sync with the package.


def embed(match: re.Match[str]) -> str:
    source = (SKILLS_SRC / match.group(1) / "SKILL.md").read_text().rstrip()
    return f"````markdown\n{source}\n````"


def main() -> None:
    for qmd in (BUILD_DIR / "docs").rglob("*.qmd"):
        text = qmd.read_text()
        if (new := MARKER.sub(embed, text)) != text:
            qmd.write_text(new)


if __name__ == "__main__":
    main()
