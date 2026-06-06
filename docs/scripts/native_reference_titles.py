from __future__ import annotations

import re
from pathlib import Path

BUILD_DIR = Path(__file__).resolve().parents[1]
SPAN_TITLE = re.compile(r'^title: ["\']?\\?\[(.+?)\]\{([^}]*)\}["\']?\s*$', re.MULTILINE)

# Pandoc's emphasis resolver backtracks exponentially when the hidden
# navigation envelope (one paragraph holding every sidebar title) contains
# many "__dunder__" emphasis candidates inside bracketed spans; without this,
# rendering any sidebar-listed page hangs pandoc at 100% CPU
# (jgm/pandoc#11687, quarto-dev/quarto-cli#14576). Rewriting the generated
# "[X]{.doc-*}" titles as raw pandoc-native inlines (a Quarto AST feature,
# suggested on the issue) hands the writers a ready-made Span: linear to
# parse and styled exactly like the original in every output format.


def native_title(match: re.Match[str]) -> str:
    tokens = match.group(2).split()
    if not all(token.startswith(".") for token in tokens):
        raise ValueError(f"non-class attr in generated title: {match.group(0)!r}")
    classes = ",".join(f'"{token[1:]}"' for token in tokens)
    return f"""title: '`Span ("",[{classes}],[]) [Str "{match.group(1)}"]`{{=pandoc-native}}'"""


def main() -> None:
    for qmd in (BUILD_DIR / "reference").rglob("*.qmd"):
        text = qmd.read_text()
        new, n = SPAN_TITLE.subn(native_title, text)
        if n:
            qmd.write_text(new)


if __name__ == "__main__":
    main()
