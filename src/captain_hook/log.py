from __future__ import annotations

import logging
import sys
from pathlib import Path

from captain_hook.session import session_hash

LOG_ROOT = Path.home() / ".cache" / "captain-hook" / "logs"


def setup_logging(transcript_path: str | None) -> None:
    session_id = session_hash(transcript_path) if transcript_path else "unknown"
    log_dir = LOG_ROOT
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
