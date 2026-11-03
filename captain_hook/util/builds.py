from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook.util import reqenv

if TYPE_CHECKING:
    from captain_hook.cmd import Call

LOAD_PER_CORE = "HOOKS_ADMISSION_LOAD_PER_CORE"
ROUTE_PROBE = "HOOKS_ADMISSION_ROUTE_PROBE"
ROUTE_PROBE_TIMEOUT = 5


@dataclass(frozen=True, slots=True)
class HostLoad:
    load: float
    cores: int
    limit: float

    @classmethod
    def sample(cls) -> HostLoad:
        return cls(os.getloadavg()[0], os.cpu_count(), float(reqenv.getenv(LOAD_PER_CORE, "1")))

    @property
    def saturated(self) -> bool:
        return self.load >= self.limit * self.cores


def is_local_build(call: Call) -> bool:
    match call.name, call.args:
        case "go", ("build" | "test" | "vet", *_) | ("-C", _, "build" | "test" | "vet", *_):
            return True
        case "cargo", ("build" | "check" | "test", *_):
            return True
        case "cargo", (toolchain, "build" | "check" | "test", *_) if toolchain.startswith("+"):
            return True
        case "go", ("run", program, *_):
            return program.partition("@")[0] == "golang.org/x/tools/cmd/deadcode"
        case _:
            return False


def routes_off_host(call: Call) -> bool:
    probe = shlex.split(reqenv.getenv(ROUTE_PROBE, ""))
    if not probe or call.wrappers or "/" in call.command.executable:
        return False
    env = reqenv.env_map() | dict(call.source.env)
    if shutil.which(probe[0], path=env.get("PATH")) is None:
        return False
    done = subprocess.run(
        [*probe, call.name, *call.args],
        capture_output=True,
        cwd=call.cwd or reqenv.cwd(),
        env=env,
        timeout=reqenv.clamp_timeout(ROUTE_PROBE_TIMEOUT),
        check=False,
    )
    return done.returncode == 0
