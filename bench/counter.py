"""The machine's own execve counter, read straight out of the kernel.

``security.mac.asp.stats.exec_hook_count`` is a monotonic system-wide count of MAC
exec-hook invocations — one per ``execve`` — and unlike dtrace, ``eslogger`` and the
BSM audit trail it is readable without root on a SIP-enabled machine. Reading it
costs no process of its own, so a measurement window does not perturb what it
measures. It is system-wide, though: a window also counts whatever else the machine
execs while it is open, which is what :mod:`bench.measure` pairs idle windows against.
"""

from __future__ import annotations

import ctypes
import ctypes.util

EXEC_HOOK_COUNT = b"security.mac.asp.stats.exec_hook_count"
EXEC_HOOK_SLEEP = b"security.mac.asp.stats.exec_hook_sleep_time"

LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def counter(name: bytes) -> int:
    value = ctypes.c_uint64()
    size = ctypes.c_size_t(ctypes.sizeof(value))
    if LIBC.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), f"sysctlbyname {name.decode()}")
    return value.value


def execs() -> int:
    """The machine's execve count since boot."""
    return counter(EXEC_HOOK_COUNT)


def endpoint_security_toll_us() -> float:
    """Mean microseconds an exec has spent parked in the endpoint-security hook since boot."""
    return counter(EXEC_HOOK_SLEEP) / 1000 / counter(EXEC_HOOK_COUNT)
