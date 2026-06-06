from __future__ import annotations

from pathlib import Path

BUILD_DIR = Path(__file__).resolve().parents[1]
EXAMPLES_SRC = BUILD_DIR.parent / "docs" / "examples"
INCLUDES_DST = BUILD_DIR / "docs" / "examples" / "_includes"


def main() -> None:
    INCLUDES_DST.mkdir(parents=True, exist_ok=True)
    for source in sorted(EXAMPLES_SRC.glob("*.py")):
        snippet = f"```python\n{source.read_text().rstrip()}\n```\n"
        (INCLUDES_DST / f"{source.stem}.qmd").write_text(snippet)


if __name__ == "__main__":
    main()
