from __future__ import annotations

import re


def kebab(name: str) -> str:
    """Convert a TitleCamelCase name to kebab-case (``NoNestedImports`` -> ``no-nested-imports``)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()
