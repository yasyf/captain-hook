from __future__ import annotations

import json
import math
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from captain_hook import Allow, Event, HookResult, Input, SkillCall, Tool, Warn, on
from captain_hook.annotations import COMMENT_TOKEN, dispatch_pairs, pair
from captain_hook.prompt import Prompt
from captain_hook.types import Action
from captain_hook.util import reqenv
from captain_hook.util.caching import ttl_cache

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from captain_hook import BaseHookEvent

LABEL = "rules_nudge"
ACK = "rules-ack"
SHIP_COMMANDS = (("ccx", "vcs", "ship"), ("ccx", "vcs", "stack", "submit"), ("gt", "submit"), ("git", "push"))
SUBMIT_SKILLS = frozenset({"submit-pr", "open-pr", "open-pr:open-pr"})
RULINGS_LABEL = "scope:durable"
RULINGS_TTL = 300.0
CLI_TIMEOUT = 5
JUDGE_DEADLINE = 25.0
SHORTLIST = 40
RULING_CHARS = 800
DIFF_CHARS = 24_000
CONTEXT_CHARS = 60_000
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
TERM = re.compile(r"[a-z][a-z0-9]{3,}")
LEDGER_RULING = (
    "ec2881eeea7fb693a984b9bebadede555c540745",
    "Is the DynamoDB release ledger ever a source of truth for release-pipeline decisions?",
    "No. A stack's applied commit is read from the Pulumi stack; the ledger only records and renders.",
)
SLACK_RULING = (
    "2d2c6c8ad22484873c3fee99ba9162f7774a1a32",
    "Which model runs the Slack write hook's judge?",
    "gpt-6-luna at low effort is the default small model for every judge.",
)
LEDGER_DIFF = (
    "diff --git a/go/ci/internal/release/resolve.go b/go/ci/internal/release/resolve.go\n"
    "@@ -10,3 +10,4 @@\n"
    "+\tapplied := ledger.LastApplied(stack) // the release ledger decides the applied commit\n"
)
BRANCH = {
    "git symbolic-ref": "origin/dev\n",
    "git merge-base": "4b825dc642cb6eb9a060e54bf8d69288fbee4904\n",
    "git diff": LEDGER_DIFF,
    "ccn answer list": json.dumps(
        [{"id": i, "title": t, "body": b, "tags": [RULINGS_LABEL]} for i, t, b in (LEDGER_RULING, SLACK_RULING)]
    ),
}
HIT = {"contradictions": [{"ruling": "ec2881e", "sentence": "resolve.go reads the applied commit from the ledger."}]}

PROMPT = """You check a code change against the owner's durable rulings: decisions the owner
made once and expects every later change to keep. `<diff>` is the branch's change against
trunk; `<durable_rulings>` lists the rulings most related to it, each with its id, the
question it answered, and the owner's answer. `<pending_tool_call>` is the ship or push about
to run.

Your one job: name every ruling this diff contradicts. A diff contradicts a ruling when the
code it adds or keeps does what the ruling forbids, or removes what the ruling requires:
reading a source of truth the ruling rejects, re-adding a mechanism the ruling deleted,
adding a flag, fallback, or special case the ruling bans, or routing work to a model or
tool the ruling rules out.

Do NOT report:
- A ruling the diff merely touches the same area as, without going against it.
- A ruling the diff carries out, or moves the code toward.
- Lines the diff deletes, unless deleting them is what breaks the ruling.
- A suspicion you cannot point to in the diff.

For each contradiction give the ruling id exactly as shown and one sentence naming what in
the diff goes against it. Return an empty list when nothing contradicts a ruling; that is
the common answer."""


class Contradiction(BaseModel):
    ruling: str
    sentence: str


class RulingsVerdict(BaseModel):
    contradictions: list[Contradiction] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Ruling:
    id: str
    title: str
    body: str

    @property
    def short(self) -> str:
        return self.id[:7]

    @property
    def terms(self) -> frozenset[str]:
        return terms(f"{self.title}\n{self.body}")

    def render(self) -> str:
        return f'<ruling id="{self.short}">\n{self.title}\n{self.body[:RULING_CHARS]}\n</ruling>'


@dataclass(frozen=True, slots=True)
class Block:
    tag: str
    text: str
    required: bool = True

    def content(self, evt: BaseHookEvent) -> str | None:
        return self.text


@dataclass(frozen=True, slots=True)
class Review:
    diff: str
    rulings: tuple[Ruling, ...]

    @property
    def contexts(self) -> tuple[Block, ...]:
        return Block("diff", self.diff), Block("durable_rulings", "\n".join(r.render() for r in self.rulings))

    def named(self, verdict: RulingsVerdict) -> list[tuple[Ruling, str]]:
        by_short = {r.short: r for r in self.rulings}
        return [
            (by_short[c.ruling[:7]], c.sentence.strip())
            for c in verdict.contradictions
            if c.ruling[:7] in by_short and c.sentence.strip()
        ]


def terms(text: str) -> frozenset[str]:
    return frozenset(TERM.findall(CAMEL.sub(" ", text).lower()))


def run(cwd: str, *argv: str) -> str | None:
    done = subprocess.run(
        argv, capture_output=True, text=True, cwd=cwd, timeout=CLI_TIMEOUT, env=reqenv.env_map(), check=False
    )
    return done.stdout if done.returncode == 0 and done.stdout.strip() else None


@ttl_cache(RULINGS_TTL)
def durable_rulings(cwd: str) -> tuple[Ruling, ...]:
    out = run(cwd, "ccn", "answer", "list", "--label", RULINGS_LABEL, "--json", "--limit", "0")
    return tuple(Ruling(a["id"], a["title"], a.get("body") or "") for a in json.loads(out)) if out else ()


def shortlist(rulings: Sequence[Ruling], diff: str, size: int = SHORTLIST) -> tuple[Ruling, ...]:
    wanted = terms(diff)
    frequency = Counter(term for r in rulings for term in r.terms)
    weight = {term: math.log(len(rulings) / count) for term, count in frequency.items()}
    scored = [(sum(weight[t] for t in r.terms & wanted), r) for r in rulings]
    return tuple(r for score, r in sorted(scored, key=lambda s: -s[0])[:size] if score > 0)


def acks(texts: Iterable[str]) -> set[str]:
    text = "\n".join(texts)
    found = chain(dispatch_pairs(text), (pair(*m.groups("")) for m in COMMENT_TOKEN.finditer(text)))
    return {value for key, value in found if key == ACK and value}


def shipping(evt: BaseHookEvent) -> bool:
    match evt.input:
        case SkillCall(skill=skill):
            return skill in SUBMIT_SKILLS
    return any((call.name, *call.args)[: len(verb)] == verb for call in evt.command.calls() for verb in SHIP_COMMANDS)


def review(evt: BaseHookEvent) -> Review | None:
    cwd = str(evt.cwd or reqenv.cwd())
    if not (trunk := run(cwd, "git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD")):
        return None
    if not (base := run(cwd, "git", "merge-base", "HEAD", trunk.strip())):
        return None
    diff = run(cwd, "git", "diff", "--no-color", "--no-ext-diff", base.strip()) or ""
    if not diff.strip() or not (rulings := durable_rulings(cwd)):
        return None
    messages = run(cwd, "git", "log", "--format=%B", f"{base.strip()}..HEAD") or ""
    acked = acks((messages, evt.command.raw if evt.command else "", getattr(evt.input, "args", None) or ""))
    live = [r for r in rulings if not any(r.id.startswith(a) for a in acked)]
    clipped = diff if len(diff) <= DIFF_CHARS else diff[:DIFF_CHARS] + f"\n…(+{len(diff) - DIFF_CHARS}ch)"
    return Review(clipped, shortlist(live, diff)) if live else None


def nudge(found: list[tuple[Ruling, str]]) -> HookResult:
    ids = " ".join(f"`{r.short}`" for r, _ in found)
    return HookResult(
        action=Action.warn,
        approve=False,
        message=f"This change may contradict the standing decisions {ids}. Read each with `ccn show <id>` and fix "
        f"the change, or add `ccx: {ACK}=<id>` to the commit message when it is deliberate.",
        system_message="rules_nudge: " + "; ".join(f"{r.short} ({r.title}): {sentence}" for r, sentence in found),
    )


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash", "Skill")],
    max_fires=None,
    tests={
        Input(command="ccx vcs ship -m 'release: read applied commit'", commands=BRANCH, llm=HIT): Warn(
            pattern="`ec2881e`"
        ),
        Input(tool="Skill", tool_input={"skill": "submit-pr"}, commands=BRANCH, llm=HIT): Warn(pattern="ec2881e"),
        Input(command="git push origin HEAD", commands=BRANCH, llm={"contradictions": []}): Allow(),
        Input(
            command="git push origin HEAD",
            commands=BRANCH,
            llm={"contradictions": [{"ruling": "0000000", "sentence": "an id the shortlist never offered"}]},
        ): Allow(),
        Input(
            command="ccx vcs stack submit",
            commands=BRANCH | {"git log": "release: read applied commit\n\nccx: rules-ack=ec2881e\n"},
            llm=HIT,
        ): Allow(),
        Input(command="ccx vcs ship -m 'x'  # ccx:rules-ack=ec2881e", commands=BRANCH, llm=HIT): Allow(),
        Input(command="git status", commands=BRANCH, llm=HIT): Allow(),
        Input(tool="Skill", tool_input={"skill": "pr-loop"}, commands=BRANCH, llm=HIT): Allow(),
    },
)
def rules_nudge(evt: BaseHookEvent) -> HookResult | None:
    from captain_hook.primitives.llm import llm_evaluate

    if not shipping(evt) or (found := review(evt)) is None or not found.rulings:
        return None
    with reqenv.deadline_in(JUDGE_DEADLINE):
        verdict = llm_evaluate(
            evt,
            Prompt().system(PROMPT),
            RulingsVerdict,
            hook=LABEL,
            contexts=found.contexts,
            max_context=CONTEXT_CHARS,
            once_per_turn=False,
            evidence=False,
        )
    named = found.named(verdict) if isinstance(verdict, RulingsVerdict) else []
    return nudge(named) if named else None
