"""The Captain Hook desktop helper's Python seam: paths, the signed bridge, and the CLI.

The helper itself is a signed macOS app; this package is the ``capt-hook`` side that
addresses it through the app-embedded signed bridge (:mod:`captain_hook.helper.client`)
and the ``capt-hook helper`` command group (:mod:`captain_hook.helper.cli`).
"""

from __future__ import annotations

FORMULA = "yasyf/tap/captain-hook"
