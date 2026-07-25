from __future__ import annotations

import importlib.metadata
import os
import sys

from captain_hook.daemon.context import ContextIO
from captain_hook.worker.runtime import ProductRuntime
from captain_hook.worker.service import WorkerService

# Workers inherit the hermetic launchd PATH, hiding user CLIs (claude, codex, gh)
# from shutil.which and hook subprocesses; daemonkit forbids overriding it at spawn.
PATH_FALLBACKS = ("~/.local/bin", "~/.bun/bin", "/opt/homebrew/bin", "/usr/local/bin")


def augment_path() -> None:
    entries = os.environ.get("PATH", "").split(os.pathsep)
    missing = [
        path
        for candidate in PATH_FALLBACKS
        if (path := os.path.expanduser(candidate)) not in entries and os.path.isdir(path)
    ]
    if missing:
        os.environ["PATH"] = os.pathsep.join(entries + missing)


def main() -> None:
    augment_path()
    protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    fallback = sys.stderr
    sys.stdout = ContextIO("stdout", fallback)
    sys.stderr = ContextIO("stderr", fallback)
    runtime = ProductRuntime()
    try:
        WorkerService(
            sys.stdin.buffer,
            protocol_output,
            build=importlib.metadata.version("capt-hook"),
            dispatch=runtime.dispatch,
        ).run()
    finally:
        runtime.close()
        protocol_output.close()


if __name__ == "__main__":
    main()
