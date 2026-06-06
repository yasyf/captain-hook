from __future__ import annotations

import re
from pathlib import Path

BUILD_DIR = Path(__file__).resolve().parents[1]
SPAN_TITLE = re.compile(r'^title: ["\']?\\?\[(.+?)\]\{[^}]*\}["\']?\s*$', re.MULTILINE)

# Pandoc's emphasis resolver backtracks exponentially when the hidden
# navigation envelope (one paragraph holding every sidebar title) contains
# many "__dunder__" emphasis candidates inside bracketed spans. Rewriting
# the generated "[X]{.doc-*}" titles as plain code spans keeps the envelope
# linear to parse; without this, rendering any sidebar-listed page hangs
# pandoc at 100% CPU (reproduced on quarto 1.9.38 and 1.10.8, 2026-06-06).


def main() -> None:
    for qmd in (BUILD_DIR / "reference").rglob("*.qmd"):
        text = qmd.read_text()
        new, n = SPAN_TITLE.subn(lambda m: f'title: "`{m.group(1)}`"', text)
        if n:
            qmd.write_text(new)


if __name__ == "__main__":
    main()
