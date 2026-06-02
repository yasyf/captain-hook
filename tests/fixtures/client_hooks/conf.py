from __future__ import annotations

from captain_hook.settings import HooksSettings


class Settings(HooksSettings):
    test_command: str = "pytest -q"
    require_review_before_stop: bool = True
