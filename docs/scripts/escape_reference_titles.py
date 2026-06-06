from __future__ import annotations

import re
from pathlib import Path

BUILD_DIR = Path(__file__).resolve().parents[1]
SPAN_TITLE = re.compile(r'^title: ["\']?\\?\[(.+?)\]\{([^}]*)\}["\']?\s*$', re.MULTILINE)

# Pandoc's emphasis resolver backtracks exponentially when the hidden
# navigation envelope (one paragraph holding every sidebar title) contains
# many "__dunder__" emphasis candidates inside bracketed spans; without this,
# rendering any sidebar-listed page hangs pandoc at 100% CPU
# (jgm/pandoc#11687, quarto-dev/quarto-cli#14576). Escaping the underscores
# keeps the envelope linear to parse while preserving the styled .doc-* span.
# Single-quoted YAML because \_ is an invalid escape in double-quoted scalars.


def main() -> None:
    for qmd in (BUILD_DIR / "reference").rglob("*.qmd"):
        text = qmd.read_text()
        new, n = SPAN_TITLE.subn(
            lambda m: f"title: '[{m.group(1).replace('_', r'\_')}]{{{m.group(2)}}}'", text
        )
        if n:
            qmd.write_text(new)


if __name__ == "__main__":
    main()
