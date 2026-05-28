"""Per-session captain-hook logging: writes one log file per session under the configured log dir."""
from __future__ import annotations

import logging
import sys

from captain_hook.session import session_hash
from captain_hook.settings import resolve_log_dir


def setup_logging(transcript_path: str | None) -> None:
    """Attach a per-session file handler plus a stderr WARNING handler to the captain_hook logger."""
    session_id = session_hash(transcript_path) if transcript_path else "unknown"
    log_dir = resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{session_id}.log"

    root = logging.getLogger("captain_hook")
    root.setLevel(logging.DEBUG)

    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == str(log_file) for h in root.handlers):
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stderr for h in root.handlers):
        root.addHandler(stderr_handler)
