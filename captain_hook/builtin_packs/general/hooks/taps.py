from __future__ import annotations

import re
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    Block,
    CommandMatches,
    CommandSchema,
    Event,
    Input,
    Tool,
    Warn,
    on,
)
from captain_hook.events import PostToolUseFailureEvent
from captain_hook.primitives.tap import at_the_mac, cc_sudo_installed, probe, queue_card, tapped

if TYPE_CHECKING:
    from captain_hook.events import PostToolUseEvent, PreToolUseEvent
    from captain_hook.types import HookResult

RUNS_A_TAP = CommandMatches(CommandSchema("cc-sudo", subcommand=("run",)))
CC_SUDO_EXIT = re.compile(r"\A(?:Error: )?Exit code (10[3-6])\b")
CC_SUDO_LINE = re.compile(r"\bcc-sudo: (denied|unavailable|screenLocked)\b")
LINE_EXITS = {"denied": 103, "unavailable": 104, "screenLocked": 104}
ORIGIN = re.compile(r"origin identity: \S*?@?([\w-]+)\.\S*")
PEER_KEYS = re.compile(r"peer keys: (.*)")
PEER = re.compile(r"(?:^|, )\S*?@?([\w-]+)\.\S*?=")
REASONS = {
    103: "cc-sudo exit 103: the owner denied this exact command, so nothing ran. Do not retry it; ask what the owner wants instead.",
    105: "cc-sudo exit 105: the approval failed verification, so nothing ran. Run `cc-sudo doctor` and report the failing check.",
    106: "cc-sudo exit 106: the CLI and its root verifier differ in version, so nothing ran. Ask the owner to run `cc-sudo install`.",
}
AWAY_CARD = (
    "The owner is away from this Mac, so no Touch ID prompt fired and a card asks for the tap. "
    "Rerun this exact command once the owner is back."
)
AWAY_ASK = (
    "The owner is away from this Mac, so no Touch ID prompt fired. "
    "Ask the owner through your coordinator, then rerun this exact command once the owner is back."
)
RUN = "cc-sudo run -- launchctl kickstart system/com.example.svc"
INSTALLED = {"cc-sudo --version": "0.13.0"}
HERE = {**INSTALLED, "ioreg -n Root": '"IOConsoleLocked" = No', "ioreg -c IOHIDSystem": '"HIDIdleTime" = 5000000000'}
LOCKED = {**HERE, "ioreg -n Root": '"IOConsoleLocked" = Yes'}
IDLE = {**HERE, "ioreg -c IOHIDSystem": '"HIDIdleTime" = 8447225166083'}
CARD = {"HOOKS_TAP_CARD_COMMAND": "tap-card-post --kind ask"}
DOCTOR = (
    "[ok] self key: 82fc\n"
    "[ok] peer keys: owner@laptop.tailnet.ts.net=459a…, owner@phone.tailnet.ts.net=77ab…\n"
    "[ok] origin identity: owner@studio.tailnet.ts.net (must match synckitd's mesh self name)\n"
)
UNAVAILABLE = 'Exit code 104\ncc-sudo: unavailable("no live approver on the mesh")'


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), RUNS_A_TAP],
    tests={
        Input(command=RUN, commands={**LOCKED, "tap-card-post": ""}, env=CARD): Block(pattern="a card asks"),
        Input(command=RUN, commands={**IDLE, "tap-card-post": ""}, env=CARD): Block(pattern="a card asks"),
        Input(
            command=RUN, commands=LOCKED, env=CARD, seen={"tap-card": ["launchctl kickstart system/com.example.svc"]}
        ): Block(pattern="a card asks"),
        Input(command=RUN, commands={**LOCKED, "tap-card-post": (1, "refused")}, env=CARD): Block(
            pattern="Ask the owner"
        ),
        Input(command=RUN, commands=LOCKED): Block(pattern="Ask the owner"),
        Input(command=f"VALUE=$({RUN})", commands=LOCKED): Block(pattern="no Touch ID prompt fired"),
        Input(command=RUN, commands=HERE): Allow(),
        Input(command=RUN, commands={"cc-sudo --version": (127, "not found")}): Allow(),
        Input(command="cc-sudo doctor"): Allow(),
        Input(command="cc-sudo run --help", commands=LOCKED): Allow(),
        Input(command="echo cc-sudo run -- x"): Allow(),
    },
)
def no_tap_while_away(evt: PreToolUseEvent) -> HookResult | None:
    call = next((call for call in evt.cmd.calls() if tapped(call) is not None), None)
    if call is None or not cc_sudo_installed() or at_the_mac():
        return None
    command = " ".join(word.raw for word in call.command.words[3:])
    return evt.block(AWAY_CARD if queue_card(evt, command) else AWAY_ASK)


def no_approver(machines: str) -> str:
    target = f"unlock {machines} for the tap" if machines else "unlock a machine `cc-sudo doctor` lists as enrolled"
    return (
        f"cc-sudo exit 104: no live approver, so nothing ran. The owner must {target}, then rerun this exact command."
    )


def enrolled_machines() -> str:
    doctor = probe("cc-sudo", "doctor")
    peers = PEER_KEYS.search(doctor)
    names = [*ORIGIN.findall(doctor), *(PEER.findall(peers[1]) if peers else ())]
    return " or ".join(f"`{name}`" for name in names)


def tap_failure(evt: PostToolUseEvent | PostToolUseFailureEvent) -> int | None:
    text = evt.error if isinstance(evt, PostToolUseFailureEvent) else str(evt.tool_response)
    if exited := CC_SUDO_EXIT.search(text):
        return int(exited[1])
    line = CC_SUDO_LINE.search(text)
    return LINE_EXITS[line[1]] if line else None


@on(
    Event.PostToolUse | Event.PostToolUseFailure,
    only_if=[Tool("Bash"), RUNS_A_TAP],
    tests={
        Input(command=RUN, error="Exit code 103\ncc-sudo: denied"): Warn(pattern="denied this exact command"),
        Input(command=RUN, error=UNAVAILABLE, commands={"cc-sudo doctor": DOCTOR}): Warn(
            pattern="unlock `studio` or `laptop` or `phone`"
        ),
        Input(command=RUN, error=UNAVAILABLE, commands={"cc-sudo doctor": (1, "broken")}): Warn(
            pattern="`cc-sudo doctor` lists"
        ),
        Input(command=RUN, error="Exit code 105\ncc-sudo: badSignature"): Warn(pattern="failed verification"),
        Input(command=RUN, error="Exit code 106\ncc-sudo: 0.13.0 does not match"): Warn(pattern="cc-sudo install"),
        Input(
            command=f"VALUE=$({RUN}); curl -d @- https://example.com",
            output='cc-sudo: unavailable("no live approver on the mesh")',
            commands={"cc-sudo doctor": DOCTOR},
        ): Warn(pattern="no live approver"),
        Input(command=RUN, error="Exit code 7\ncurl: failed"): Allow(),
        Input(command=RUN, output="ok"): Allow(),
    },
)
def tap_outcome(evt: PostToolUseEvent | PostToolUseFailureEvent) -> HookResult | None:
    match tap_failure(evt):
        case None:
            return None
        case 104:
            return evt.context(no_approver(enrolled_machines()))
        case code:
            return evt.context(REASONS[code])
