from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook.util import reqenv

if TYPE_CHECKING:
    from captain_hook.cmd import Call

LOAD_PER_CORE = "HOOKS_ADMISSION_LOAD_PER_CORE"
ROUTE_PROBE = "HOOKS_ADMISSION_ROUTE_PROBE"
ROUTE_PROBE_TIMEOUT = 5
BUSY_SAMPLE_SECONDS = 0.5
HOST_CPU_LOAD_INFO = 3
HOST_CPU_STATES = 4
HOST_CPU_IDLE = 2


@dataclass(frozen=True, slots=True)
class HostLoad:
    load: float
    cores: int
    limit: float

    @classmethod
    def sample(cls) -> HostLoad:
        cores, limit = os.cpu_count() or 1, float(reqenv.getenv(LOAD_PER_CORE, "1"))
        return cls(busy_fraction() * cores if 0 < limit <= 1 else 0.0, cores, limit)

    @property
    def saturated(self) -> bool:
        return self.load >= self.limit * self.cores


def busy_fraction() -> float:
    """The share of CPU time spent working over a short sample.

    The load average counts threads blocked on I/O and takes a minute to decay after a burst, so
    it can read 0.8 per core while three quarters of the CPU sits idle.
    """
    idle, total = cpu_ticks()
    time.sleep(BUSY_SAMPLE_SECONDS)
    later_idle, later_total = cpu_ticks()
    elapsed = later_total - total
    return 1 - (later_idle - idle) / elapsed if elapsed else 0.0


def cpu_ticks() -> tuple[int, int]:
    """Idle and total CPU ticks since boot."""
    if sys.platform == "darwin":
        import ctypes

        system = ctypes.CDLL("/usr/lib/libSystem.dylib")
        system.mach_host_self.restype = ctypes.c_uint
        states = (ctypes.c_uint * HOST_CPU_STATES)()
        count = ctypes.c_uint(HOST_CPU_STATES)
        system.host_statistics(system.mach_host_self(), HOST_CPU_LOAD_INFO, states, ctypes.byref(count))
        return states[HOST_CPU_IDLE], sum(states)
    with open("/proc/stat") as stat:
        ticks = [int(tick) for tick in stat.readline().split()[1:]]
    return ticks[3] + ticks[4], sum(ticks)


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
