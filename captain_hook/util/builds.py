from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook.util import reqenv

if TYPE_CHECKING:
    from captain_hook.cmd import Call

LOAD_PER_CORE = "HOOKS_ADMISSION_LOAD_PER_CORE"


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
