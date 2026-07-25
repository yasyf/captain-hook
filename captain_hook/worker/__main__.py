from __future__ import annotations

import importlib.metadata
import os
import sys

from captain_hook.daemon.context import ContextIO
from captain_hook.worker.runtime import ProductRuntime
from captain_hook.worker.service import WorkerService


def main() -> None:
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
