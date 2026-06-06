from __future__ import annotations

import re
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parents[2] / "great-docs" / "_site"
LOADER = re.compile(
    r"<script>\(function\(\)\{var s=document\.createElement\('script'\);"
    r".*?color-swatch\.js.*?\}\)\(\)</script>"
)

# Replaces great-docs' runtime color-swatch.js loader with a depth-correct
# static script tag. The runtime loader strips exactly two path segments from
# the canonical URL, which 404s on any page not exactly one directory deep
# (e.g. the homepage). Runs over the built site, after `great-docs build`.


def main() -> None:
    for page in SITE_DIR.rglob("*.html"):
        depth = len(page.relative_to(SITE_DIR).parts) - 1
        text = page.read_text()
        tag = f'<script src="{"../" * depth}color-swatch.js"></script>'
        if (new := LOADER.sub(tag, text)) != text:
            page.write_text(new)


if __name__ == "__main__":
    main()
