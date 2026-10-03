from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING

from captain_hook.util import proc
from captain_hook.util.proc import Unreadable

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from captain_hook.util.proc import ProcessRow


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    ppid: int
    pgid: int
    start_unix: int
    argv: tuple[str, ...]
    cwd: str | None

    @classmethod
    def from_payload(cls, process: Mapping[str, object]) -> ProcessIdentity:
        match process:
            case {
                "pid": int() as pid,
                "ppid": int() as ppid,
                "pgid": int() as pgid,
                "start_unix": int() as start_unix,
                "argv": list() as argv,
            } if all(isinstance(arg, str) for arg in argv):
                return cls(pid, ppid, pgid, start_unix, tuple(argv), payload_cwd(process))
            case _:
                raise ValueError(f"malformed process payload: {sorted(process)}")

    @property
    def key(self) -> str:
        return f"{self.pid}:{self.start_unix}"

    @property
    def command(self) -> str:
        return " ".join(self.argv)


def payload_cwd(process: Mapping[str, object]) -> str | None:
    match process.get("cwd"):
        case str() as cwd if cwd:
            return cwd
        case None | "":
            return None
        case other:
            raise ValueError(f"malformed process cwd: {other!r}")


def start_unix(row: ProcessRow) -> int:
    return int(row.started.replace(tzinfo=UTC).timestamp())


def mismatch(identity: ProcessIdentity, row: ProcessRow) -> Unreadable | None:
    if row.ppid != identity.ppid:
        return Unreadable(f"pid {identity.pid} was reparented: ppid {row.ppid} now, {identity.ppid} when tracked.")
    if row.pgid != identity.pgid:
        return Unreadable(f"pid {identity.pid} changed group: pgid {row.pgid} now, {identity.pgid} when tracked.")
    if start_unix(row) != identity.start_unix:
        return Unreadable(f"pid {identity.pid} was reused: its start time differs from the tracked process.")
    if row.command.split() != identity.command.split():
        return Unreadable(f"pid {identity.pid} exec'd a different program: `{row.command}` is not the tracked command.")
    return None


def verify_cwd(
    identity: ProcessIdentity, *, process_cwd: Callable[[int], str | None] = proc.process_cwd
) -> Unreadable | None:
    if identity.cwd is None:
        return Unreadable(f"pid {identity.pid}: no recorded cwd to verify.")
    if (cwd := process_cwd(identity.pid)) is None:
        return Unreadable(f"pid {identity.pid}: cwd unreadable.")
    if cwd != identity.cwd:
        return Unreadable(f"pid {identity.pid}: cwd changed from `{identity.cwd}` to `{cwd}`.")
    return None


def verify_row(
    identity: ProcessIdentity, *, usage_row: Callable[[int], ProcessRow | None] = proc.usage_row
) -> ProcessRow | Unreadable:
    if (row := usage_row(identity.pid)) is None:
        return Unreadable(f"pid {identity.pid} is not in the process table.")
    return mismatch(identity, row) or row


def verify(
    identity: ProcessIdentity,
    *,
    usage_row: Callable[[int], ProcessRow | None] = proc.usage_row,
    process_cwd: Callable[[int], str | None] = proc.process_cwd,
) -> ProcessRow | Unreadable:
    return verify_cwd(identity, process_cwd=process_cwd) or verify_row(identity, usage_row=usage_row)
