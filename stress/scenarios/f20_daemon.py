"""Resident-daemon gates: one worker under a fork storm, warm-vs-cold latency, SIGKILL recovery,
and stale-socket takeover — all driven through the installed ``hook``.

Every scenario plants the same hook set and fires the same ``PreToolUse`` payload, so the four
gates measure the daemon, not a moving target. The fork storm proves the flock front door
collapses 27 concurrent clients to exactly one worker (a per-session-keyed worker would spawn 27 —
the ``worker_key`` over-keying trap); the latency gate proves the warm path beats a cold CLI by more
than 2x and stays under 120ms; the kill9 gate proves a mid-stream ``SIGKILL`` never fails a client
(fallback=cold) and the next event respawns exactly one worker; the stale-preseed gate proves a
dead socket left by a crashed worker is taken over cleanly on the next client.
"""

from __future__ import annotations

import json
import signal
import socket
import threading
import time
from typing import TYPE_CHECKING

from stress.drivers.daemon import DaemonWorld, connectable, daemon_world, pid_alive, plant_hooks, send_signal
from stress.scenarios.base import Check, Scenario, ScenarioResult, Tier, check, expect

if TYPE_CHECKING:
    from stress.sandbox import Sandbox

FAMILY = "daemon"
EVENT = "PreToolUse"
HOOK_SRC = (
    "from __future__ import annotations\n\n"
    "from captain_hook import Event, Tool, deny, hook, on\n\n"
    'hook(Event.PreToolUse, only_if=[Tool("Edit")], message="edit guard")\n'
    'deny("no bash here", only_if=[Tool("Bash")])\n\n\n'
    "@on(Event.PreToolUse)\n"
    "def probe(evt):\n"
    "    return None\n"
)

FORK_WAYS = 27
HAMMER_WAYS = 10
SAMPLES = 50
P50_CEILING_MS = 120.0
DEAD_PID = 2_000_000_000


def payload(session_id: str) -> bytes:
    return json.dumps(
        {
            "session_id": session_id,
            "hook_event_name": EVENT,
            "tool_name": "Edit",
            "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"},
        }
    ).encode()


def percentile(values: list[float], p: float) -> float:
    if not (ordered := sorted(values)):
        return 0.0
    rank = (len(ordered) - 1) * (p / 100)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def no_live_socket(world: DaemonWorld) -> Check:
    return check("run dir holds no live socket after teardown", not world.live_sockets(), f"run={world.run}")


def run_fork_storm(sandbox: Sandbox) -> ScenarioResult:
    with daemon_world(sandbox) as world:
        plant_hooks(sandbox, HOOK_SRC)
        env = world.env(fallback="closed")
        rcs: dict[int, int | None] = dict.fromkeys(range(FORK_WAYS))

        def fire(i: int) -> None:
            rcs[i] = world.run_client(EVENT, payload(f"stress-storm-{i}"), env=env).returncode

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(FORK_WAYS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        codes = [rcs[i] for i in range(FORK_WAYS)]
        sockets, metas, pids = world.sockets(), world.metas(), world.worker_pids()
        world.stop()
        teardown = no_live_socket(world)
    return ScenarioResult(
        checks=(
            check(
                f"every one of {FORK_WAYS} concurrent clients exits 0 under fallback=closed",
                all(rc == 0 for rc in codes),
                f"codes={codes}",
            ),
            check(
                f"exactly 1 worker spawns for {FORK_WAYS} clients on one root (sockets/metas/pids all == 1)",
                len(sockets) == 1 and len(metas) == 1 and len(pids) == 1,
                f"sockets={[p.name for p in sockets]} metas={[p.name for p in metas]} pids={pids}",
            ),
            teardown,
        )
    )


def run_latency(sandbox: Sandbox) -> ScenarioResult:
    with daemon_world(sandbox) as world:
        plant_hooks(sandbox, HOOK_SRC)
        warm_env = world.env(fallback="closed", CAPT_HOOK_ONCE_TTL="0")
        cold_env = world.env(fallback="cold", CAPT_HOOK_ONCE_TTL="0")
        pay = payload("stress-lat")
        discard_ms, discard_rc = world.time_client(EVENT, pay, env=warm_env)  # pays boot + first discovery
        warm = [world.time_client(EVENT, pay, env=warm_env) for _ in range(SAMPLES)]
        cold = [world.time_cold(EVENT, pay, env=cold_env) for _ in range(SAMPLES)]
        warm_ms, warm_rc = [m for m, _ in warm], [rc for _, rc in warm]
        cold_ms, cold_rc = [m for m, _ in cold], [rc for _, rc in cold]
        d50, d95 = percentile(warm_ms, 50), percentile(warm_ms, 95)
        c50, c95 = percentile(cold_ms, 50), percentile(cold_ms, 95)
        perf = (
            f"daemon_p50={d50:.1f} daemon_p95={d95:.1f} cold_p50={c50:.1f} cold_p95={c95:.1f} "
            f"n_warm={SAMPLES} n_cold={SAMPLES} discard_ms={discard_ms:.0f}"
        )
        world.stop()
        teardown = no_live_socket(world)
    return ScenarioResult(
        checks=(
            check(
                "every warm event exits 0 (warm dispatch reached the worker under fallback=closed)",
                discard_rc == 0 and all(rc == 0 for rc in warm_rc),
                f"discard_rc={discard_rc} warm_rc={sorted(set(warm_rc))}",
            ),
            check("every cold event exits 0", all(rc == 0 for rc in cold_rc), f"cold_rc={sorted(set(cold_rc))}"),
            check(f"GATE daemon p50 < {P50_CEILING_MS:.0f}ms | {perf}", d50 < P50_CEILING_MS, perf),
            check(f"GATE daemon p50 < cold p50 / 2 ({c50 / 2:.1f}ms) | {perf}", d50 < c50 / 2, perf),
            teardown,
        )
    )


def run_kill9_hammer(sandbox: Sandbox) -> ScenarioResult:
    with daemon_world(sandbox) as world:
        plant_hooks(sandbox, HOOK_SRC)
        env = world.env(fallback="cold")
        warm = world.run_client(EVENT, payload("stress-k-warm"), env=env)
        pid_a = world.meta_pid(env)
        rcs: dict[int, int | None] = dict.fromkeys(range(HAMMER_WAYS))

        def fire(i: int) -> None:
            rcs[i] = world.run_client(EVENT, payload(f"stress-k-{i}"), env=env).returncode

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(HAMMER_WAYS)]
        for thread in threads:
            thread.start()
        killed = pid_a is not None and pid_alive(pid_a)
        if pid_a is not None:
            send_signal(pid_a, signal.SIGKILL)  # SIGKILL mid-stream, while the hammer threads are in flight
        for thread in threads:
            thread.join(timeout=60)
        nxt = world.run_client(EVENT, payload("stress-k-next"), env=env)
        pid_b = world.meta_pid(env)
        codes = [rcs[i] for i in range(HAMMER_WAYS)]
        sockets, live, pids = world.sockets(), world.live_sockets(), world.worker_pids()
        # Capture liveness before teardown — world.stop() SIGKILLs pid_b, so the check must read it here.
        respawned_one = pid_b is not None and pid_b != pid_a and pid_alive(pid_b) and len(pids) == 1
        world.stop()
        teardown = no_live_socket(world)
    return ScenarioResult(
        checks=(
            check(
                "worker established, then SIGKILLed mid-stream",
                warm.returncode == 0 and pid_a is not None and killed,
                f"warm_rc={warm.returncode} pid_a={pid_a} killed={killed}",
            ),
            check(
                "every hammered client exits 0 under fallback=cold despite the mid-stream SIGKILL",
                warm.returncode == 0 and nxt.returncode == 0 and all(rc == 0 for rc in codes),
                f"warm={warm.returncode} next={nxt.returncode} codes={codes}",
            ),
            check(
                "next event respawned exactly one fresh worker",
                respawned_one,
                f"pid_a={pid_a} pid_b={pid_b} worker_pids={pids}",
            ),
            check(
                "no stranded socket: exactly one live socket afterward",
                len(sockets) == 1 and len(live) == 1,
                f"sockets={[p.name for p in sockets]} live={live}",
            ),
            teardown,
        )
    )


def run_stale_preseed(sandbox: Sandbox) -> ScenarioResult:
    with daemon_world(sandbox) as world:
        plant_hooks(sandbox, HOOK_SRC)
        env = world.env(fallback="closed")
        sock, meta = world.sock_path(env), world.meta_path(env)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(sock))
        dead.close()  # leaves a stale, unconnectable socket node — a crashed worker's leftover
        meta.write_text(
            json.dumps(
                {
                    "pid": DEAD_PID,
                    "root": str(world.root),
                    "build": "stale",
                    "version": "0",
                    "protocol": 1,
                    "socket": str(sock),
                    "started_at": time.time() - 3600,
                }
            )
        )
        preseeded = sock.exists() and meta.exists() and not connectable(str(sock))
        first = world.run_client(EVENT, payload("stress-stale"), env=env)
        pid = world.meta_pid(env)
        live = world.live_sockets()
        # Capture liveness before teardown — world.stop() SIGKILLs the new worker.
        owner_live = pid is not None and pid != DEAD_PID and pid_alive(pid)
        world.stop()
        teardown = no_live_socket(world)
    return ScenarioResult(
        checks=(
            check("stale socket node + meta pre-seeded (socket unconnectable)", preseeded, f"sock={sock.name}"),
            check(
                "first client takes over the stale socket and serves warm (rc 0 under fallback=closed)",
                first.returncode == 0 and first.stdout != b"",
                f"rc={first.returncode} out={first.stdout[:120]!r} err={first.stderr[:200]!r}",
            ),
            check(
                "a live worker now owns the socket (meta rewritten to a live pid)",
                owner_live,
                f"pid={pid} dead_pid={DEAD_PID}",
            ),
            expect("exactly one live socket after takeover", len(live), 1),
            teardown,
        )
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(name="daemon-fork-storm", family=FAMILY, tier=Tier.OFFLINE, run=run_fork_storm),
        Scenario(name="daemon-latency", family=FAMILY, tier=Tier.OFFLINE, run=run_latency),
        Scenario(name="daemon-kill9-hammer", family=FAMILY, tier=Tier.OFFLINE, run=run_kill9_hammer),
        Scenario(name="daemon-stale-preseed", family=FAMILY, tier=Tier.OFFLINE, run=run_stale_preseed),
    )
