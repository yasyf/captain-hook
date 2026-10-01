from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    Budget,
    Event,
    FromSubagent,
    Input,
    LambdaCondition,
    RanCommand,
    Regex,
    Signal,
    Signals,
    T,
    UsedSkill,
    llm_gate,
)
from captain_hook.state import PrimitiveState

ASK_TOOLS = "AskUserQuestion|ExitPlanMode"
CLOSING_CHARS = 8000

O1_PROSE_DECISION = (
    "One decision for you, O1: the IMDS firewall is what forces three hand-rolled scripts. Keep it, and an "
    "unconfigured job fails instead of silently writing as the instance role; drop it after the Elastic work "
    "lands and stock tags replace all three. The lane recommends keep now, drop as a follow-up, and is "
    "proceeding on keep."
)

WAITING_ON_YOU = (
    "The sand rollback is approved and dispatched but not yet confirmed done. The catch-up lane is "
    "pointing `sand/latest.json` back at the previous version and will report before and after "
    "versions.\n"
    "\n"
    "**Open problems**\n"
    "- **sand 0.162.6:** it is live with an unsigned, un-notarized mac `sandsql-server`. The signing "
    "step crashed and the build still consumed its artifact. One priority PR fixes that, the sparse "
    "mac checkout that caused the crash, and a missing changelog key that breaks the sand-vscode "
    "build. Sand, sand-vscode, desktop and iris-desktop wait on it.\n"
    '- **SoFi alert:** the "Polar AML Enqueuer Failure" monitor fired at 06:46Z. The actor\'s daily '
    "batch did not run today and it logged nothing for an hour. Its errors started at 00:45Z, before "
    "any drive write, but the link is not ruled out. A read-only diagnosis lane is on it, and all "
    "drive writes to the SoFi cluster are frozen until I lift it. "
    "[Notebook](https://app.datadoghq.com/notebook/15749908).\n"
    "- **plat prod k8s:** every k8s deploy there is refused until the stack converges on its 32 "
    'pending changes. Per your "detail first", the lane is gathering the image-versus-config '
    "breakdown and the same plan for the other clusters. A lane is also adding multi-target deploys "
    "so one pinned plan can converge it without a whole-stack apply.\n"
    "\n"
    "**Progress**\n"
    "- **Foundation stack:** #27956, #27994 and #28001 landed on `dev`.\n"
    "- **Trust:** `identity/core-gbl-identity` is applied, so all four trust stacks admit the "
    "`deploy` pipeline.\n"
    "- **Stack-apply proof:** the tunnel-relay sandbox deploy is cleared to run under your ruling. An"
    " ECS task-definition replace from an image change is now a normal rollout.\n"
    "- **Mac checkout flake:** a lane is fixing the hook to mint our own GitHub App token. The "
    "catch-up lane retries meanwhile.\n"
    "- **`/tenant new`:** Platy triggers a Buildkite scaffold step that runs the existing `env-new` "
    "and `cli init`, per your pick.\n"
    "- **PR watch tooling:** the push-based watch is released and installed. Desks and lanes are "
    "being switched to it.\n"
    "\n"
    "**Waiting on you**\n"
    "- Read the design in #27868 and lift its hold.\n"
    "- Read the Flux delete list in #27953.\n"
    "- The two Vulcan clicks for the escape-hatch cut-over.\n"
    "\n"
    "The #platform-internal post stays held until every target has deployed green."
)

STILL_WITH_YOU = (
    "The landing desk confirms what I read on `dev`: #28039 and #28042 landed at 08:39Z, right after "
    "the rate-limit block lifted. 22 PRs merged in the last hour and 26 are open.\n"
    "\n"
    "**Landed this hour, per the desk**\n"
    "- The rulings PR #28021.\n"
    "- The move tool, #27979 and #28000.\n"
    "- The Go ledger, bake and picture stacks, including #27967 to #27973.\n"
    "- The db-migration Job #28025.\n"
    "- The mac checkout secret row #28051.\n"
    "- The `poetic-iris` switch #28040.\n"
    "\n"
    "**Problems**\n"
    '- **Ejection missed for 45 minutes:** the queue ejected #28006, the Go input for "which stacks '
    'does this PR affect", at 07:59Z on a red merge-group test. The watch had crashed on an over-long'
    " state filename. It has been running since 08:10Z, and the lane has the fix routed.\n"
    "- **Another rate-limit cause:** a single-PR ledger refresh was regrading all 89 tracked rows "
    "each call. That fix goes to the shared-poller lane.\n"
    "- **Four asks read as lost:** their lanes had not reported against them. The shared tooling, two"
    " tenant follow-on PRs and the env-list tests are all running, and I've told the lanes to report.\n"
    "- **Held PRs:** #28081 to #28086 show as held. I believe they are the network move PRs and have "
    "asked the desk to confirm what holds them.\n"
    "\n"
    "**Still with you:** the Flux delete list in #27953 and the design in #27868 are both held for "
    "your read."
)

VULCAN_CLICKS = (
    "The #platform-internal post is now held until every target has deployed green through "
    "`tools/deploy` at least once.\n"
    "\n"
    '- **What changed:** the announcement moves from "after one stack-apply proof" to "after the full'
    ' catch-up deploy". The catch-up lane keeps a per-target table of build links, results and '
    "monitor checks, and reports to me once when it is all green.\n"
    "- **What did not change:** the catch-up and escape-hatch cut-over lanes still start as soon as "
    "the `identity/core-gbl-identity` trust update is applied and one stack-apply target passes. The "
    'escape-hatch tenant targets count toward "all targets", so the post also waits on that cut-over '
    "and your two Vulcan clicks.\n"
    "- **Where it is recorded:** the desk instruction, the ledger ask, the plan's Phase 0 heading, my"
    " memory, and cc-notes answer `a529f9c`.\n"
    "\n"
    "A failed target or a plan showing a delete or replace comes to you straight away instead of "
    "waiting for the full table."
)

GO_GIVEN = (
    "**Phase 0 is nearly through:** PR #27949 (the `deploy` pipeline, `tools/deploy`, and the "
    "one-page skill) is green and approved, waiting only on the AI review check. The desk enqueues it"
    " the moment that completes.\n"
    "\n"
    "I gave the lane its GO for what follows the landing:\n"
    "\n"
    "1. `ci sync` to create the `deploy` Buildkite pipeline.\n"
    "2. Read-only plans of the two stacks that must trust the new pipeline (identity and ci, "
    "core-gbl-auto), with a per-stack op summary sent to me before each apply. I approve only an "
    "update-only change to trust policies naming `deploy`; any create, delete, or replace comes to "
    "you.\n"
    "3. A real `dashboard` deploy to plat-usw2-prod at dev HEAD as the proof, then the same "
    "plan-and-ask for the third identity stack Phase 0b needs.\n"
    "\n"
    "Once it's merged and proven I'll post the usage note in #platform-internal under your standing "
    "authorization.\n"
    "\n"
    "Also moving: all 27 earlier rulings are relayed; the fable lane is on the #27868 lease fix; "
    "`cleanup-writes` is running your three hand deletes; the tooling PRs in cc-skills are being "
    "merged and released; the bake lane finished with four PRs (#27970–#27973); and the area-card "
    "port launches as its own lane once the Go stacks branch is pushed."
)

OWED_REPORT = (
    "None of them has deployed through the new pipeline yet. api, restate, executor, browser, "
    "code-sandbox, tunnel, router, forge-dns and sanddb are all k8s targets. On plat prod they all "
    "live in the single `k8s/plat-usw2-prod` stack, which is where the first k8s deploy was refused.\n"
    "\n"
    "**Why they are blocked:** the pipeline keeps one guard. A deploy that pins one target refuses if"
    " the plan changes any other row. That stack has not converged since commit `682883c` and carries"
    " 32 pending changes, so every single-target deploy there is refused. The pending changes "
    "include:\n"
    "- the four api Deployments, plus a new api ConfigMap and the delete of the old one\n"
    "- the restate control-plane and data-plane workers and their register and retire commands\n"
    "- the restate reaper role\n"
    "- about a dozen helm releases, the Datadog agent, four CRD installs, a new node pool and a "
    "browser probe pod\n"
    "\n"
    "Deploying api alone with `--whole-stack` would render new config against the old restate image "
    "until restate follows. That is the config and image skew that caused release 420, so I did not "
    "approve it.\n"
    "\n"
    "**The path I approved:** a lane is adding multi-target deploys to the tool. One plan then pins "
    "api, restate, reaper, browser and the cluster-owned rows at the same commit and converges the "
    "stack under the guard. After that, single-target deploys work there.\n"
    "\n"
    "**What I owe you and don't have:**\n"
    "- The detail you asked for before deciding: which updates change an image versus config only, "
    "and what still references the deleted ConfigMap.\n"
    "- The same read-only plan for the router and SoFi clusters.\n"
    "- A per-target table for the whole catch-up order, db-migration and sandsql included.\n"
    "\n"
    "That report is overdue. I've told the desk to return it within ten minutes, with the state of "
    "the multi-target PR, and to start any k8s target whose stack is already converged."
)

APPROVAL_CONDITIONS = (
    "**I approved the tenant account-id design for code, with conditions.** The organization row "
    "creates the tenant AWS account and publishes its id. One small module reads that id from the "
    "root-account stack's state once, before any program runs. The roughly 60 places that need a "
    "plain string keep one, and nobody types an account id into a spec or form.\n"
    "\n"
    "My conditions:\n"
    '- This is the single allowed exception to "cross-stack reads go through references", limited to '
    "account ids and enforced by a test.\n"
    "- It reads through the engine's existing state access, not a hand-rolled S3 path.\n"
    "- Any role grant it needs is a separate small PR, and that apply comes to you.\n"
    "- Removing the existing typed ids is a stacked PR written now, not an open-ended follow-up.\n"
    "- The credential-resolution parts are written by the sensitive-code model.\n"
    "\n"
    "**The sandsql build failure was a code regression, not a missing permission.** #27823 moved the "
    "`sccache` setup to after the ECR role assumption, so `sccache` lost its cache bucket access. "
    "Every `dev` sandsql image prebuild has failed since, and recent v2 releases passed only by "
    "reusing a prebuilt image. The fix is one line plus a test. It repairs the old pipeline, the new "
    "`deploy` pipeline and the `dev` prebuild, and it unblocks sandsql on prod.\n"
    "\n"
    "The three tenant scaffold PRs #28071 to #28073 are green and go to the desk."
)

DESK_WAKE = (
    'Another Claude session sent a message: <agent-message from="landing-desk"> #28039 and #28042 landed '
    "as squashes on dev. </agent-message>"
)


def asked_last(evt: BaseHookEvent) -> bool:
    since = evt.ctx.t.after(tool=ASK_TOOLS)
    return evt.ctx.t.has_tool(ASK_TOOLS, subagents=False) and not (
        (count := len(since)) and since.assistant_text(count, max_per_msg=1)
    )


llm_gate(
    """You are a senior engineer watching another engineer ("the agent") end its turn. Your
one job: decide whether the agent's closing message, its last prose in the transcript, leaves
something waiting on the user in PROSE instead of asking for it with the AskUserQuestion tool.

The user's standing rule: when work needs something only the user can give (a decision, an
answer, an approval, a read of a PR, design or delete list, a click or hand step, a "go"),
the ask IS an AskUserQuestion call (2-4 concrete options, recommended first) in that same
turn. A turn that ends on it written as text, then sits idle or proceeds on a default, is a
bug. Background work still running is no excuse to defer an item the user could act on now:
a background task's result is never the user's answer.

This holds however the turn started. An orchestrator is woken by teammate or other-session
messages (`<teammate-message>`, "Another Claude session sent a message: <agent-message ...>")
and task notifications, and the user still reads the message that closes that turn: judge it
exactly like a reply to the user.

Block (block=true) when the closing message:
- puts a question or decision to the user in prose: "one decision for you", "holding for your
  pick", "still yours to decide", "your call", "up to you", "say the word", "let me know
  which", "if you'd rather", an A-or-B fork laid out as options, a deferral list parked under a
  heading such as "Still yours to decide:", "the lane recommends X and is proceeding on X" (a
  decision the user never got a real prompt for), or a "want me to ...?" / "should I ...?"
  offer closing the message;
- lists items pending on the user that the user could act on now: a "Waiting on you", "Still
  with you" or "Needs you" section, PRs or designs "held for your read" or "held for your
  word", approvals, reads or clicks the user owes ("your two Vulcan clicks"), "it comes to you
  for approval", "I'll bring it to you".

An AskUserQuestion call covers only what it asked. Match each item the closing message leaves
on the user against the questions the transcript shows were asked this turn: an item no call
asked about still counts, however many other questions the turn asked.

Do NOT block when:
- every such item was put to the user with AskUserQuestion or ExitPlanMode this turn and the
  message only reports the answer or the state it left;
- an item cannot be put to the user yet because what the user would act on is still being
  produced (a plan, a diff, a report), and the message names what it waits on and who is
  producing it: "comes to you once b2-data returns the diff, due 09:30Z";
- the message states a standing rule for a future event rather than a pending item: "any
  create, delete, or replace comes to you";
- the item is something the agent owes the user, or an approval the agent itself grants to
  its lanes; "sent to me" and "I approve" are the agent, not the user;
- the question is rhetorical and answered, quotes or reports someone else's question, or is
  addressed to a subagent, teammate, or tool rather than the user;
- the phrase sits only inside a code block or quoted text the agent is reporting (a hook
  message, a log line, a draft), not in the agent's own words to the user;
- the message reports finished work with nothing left on the user;
- the decision is on a live cc-present board (a `present` skill or `cc-present start` in this
  session) and the prose refers the user to it.

When uncertain, return block=false. Put your reasoning (under 40 words, quoting the prose)
in `reasoning`.""",
    message=(
        "Your closing message leaves something waiting on the user in prose: {reasoning} "
        "Ask for it now with AskUserQuestion (2-4 concrete options, recommended first) before ending "
        "the turn. If an item is not askable yet, say so: what it waits on and who is producing it."
    ),
    label="narrate_then_wait",
    signals=Signals(
        [
            Signal(pattern=r"(?i)\bone decision for you\b", weight=2),
            Signal(pattern=r"(?i)\b(?:holding|waiting) (?:for|on) (?:your|you)\b", weight=2),
            Signal(pattern=r"(?i)\b(?:still|now|back) (?:with|on) you\b", weight=2),
            Signal(pattern=r"(?i)\bheld (?:for|on|until) (?:your|you)\b", weight=2),
            Signal(pattern=r"(?i)\b(?:needs?|awaits?|awaiting|requires?) (?:your|you)\b", weight=2),
            Signal(
                pattern=r"(?i)\byour (?:\w+ ){0,2}(?:reads?|reviews?|approvals?|sign-?offs?|clicks?|hand[- ]?steps?)\b",
                weight=2,
            ),
            Signal(pattern=r"(?i)\byour (?:go|go-?ahead|word|nod|ok|okay)\b(?!-to)", weight=2),
            Signal(pattern=r"(?i)\bbring (?:it|them|this|that|these|those) (?:back )?to you\b", weight=2),
            Signal(
                pattern=r"(?i)\b(?:comes?|goes) (?:back )?to you for (?:an? )?(?:approval|review|read|sign-?off)\b",
                weight=2,
            ),
            Signal(pattern=r"(?i)\b(?:comes?|goes) (?:back )?to you\b", weight=1),
            Signal(pattern=r"(?i)\bowe you\b", weight=1),
            Signal(pattern=r"(?i)\b(?:let me know|tell me) (?:which|what|if|whether|how)\b", weight=2),
            Signal(pattern=r"(?i)\byour (?:call|pick|decision|choice|move|shout)\b", weight=2),
            Signal(pattern=r"(?i)\byours? to (?:decide|call|choose)\b", weight=2),
            Signal(pattern=r"(?i)\bup to you\b", weight=2),
            Signal(pattern=r"(?i)\bsay the word\b", weight=2),
            Signal(pattern=r"(?i)\bif you(?:'|’)?d (?:rather|prefer)\b", weight=2),
            Signal(pattern=r"(?i)\bleave (?:it|that|this|them) (?:up )?to you\b", weight=2),
            Signal(pattern=r"(?i)\bwhich (?:one )?(?:do|would) you (?:want|prefer|like)\b", weight=2),
            Signal(pattern=r"(?i)\b(?:should I|want me to|shall I)\b[^.?!]*[.?!]", weight=2),
            Signal(pattern=r"(?i)\brecommends?\b[^.]*\b(?:proceeding|going ahead) (?:on|with)\b", weight=2),
            Signal(pattern=r"\?\s*$", weight=1),
            Signal(pattern=r"(?im)^\s*(?:[-*]\s*)?(?:option\s+[A-D1-4]\b|\(?[a-d]\)\s)", weight=1),
        ],
        threshold=2,
        window=6,
        scope="text",
    ),
    skip_if=[
        FromSubagent(),
        LambdaCondition(lambda evt: evt.stop_hook_active),
        LambdaCondition(lambda evt: "AskUserQuestion" in evt.disallowed_tools),
        LambdaCondition(asked_last),
        UsedSkill("present", scope="session", subagents=False),
        RanCommand(Regex(r"^(?:\S*/)?cc-present start\b"), subagents=False),
    ],
    guards_waiting=False,
    once_per_turn=False,
    budget=Budget(turn_chars=CLOSING_CHARS),
    events=Event.Stop,
    tests={
        Input(transcript=[T.assistant(O1_PROSE_DECISION)]): Block(pattern="AskUserQuestion"),
        Input(transcript=[T.assistant("Holding for your pick: rebase onto dev or cherry-pick the fix?")]): Block(
            pattern="AskUserQuestion"
        ),
        Input(
            transcript=[
                T.assistant(
                    "Both stacks are green and the boot stack is four PRs deep.\n\n"
                    "Still yours to decide at the end: the api-actions pool PRs, #21840 and #21847."
                )
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[T.assistant("Still yours to decide: the api-actions pool PRs, #21840 and #21847.")],
            background_tasks=[
                {"id": "t1", "type": "subagent", "status": "running", "description": "pr-watcher on #21840"}
            ],
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.assistant("Still yours to decide: the api-actions pool PRs, #21840 and #21847."),
                *(T.assistant(T.tool("Read", file_path=f"api/src/f{n}.ts")) for n in range(8)),
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[T.assistant("The cap is 30 slots today — up to you whether we raise it or shed jobs.")]
        ): Block(pattern="AskUserQuestion"),
        Input(transcript=[T.user("what is waiting on me?"), T.assistant(STILL_WITH_YOU)]): Block(
            pattern="AskUserQuestion"
        ),
        Input(transcript=[T.user("where is the drive?"), T.assistant(VULCAN_CLICKS)]): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.user("roll back sand now"),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Roll back sand 0.162.6?"}])),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Prove the stack apply on tunnel?"}])),
                T.assistant(WAITING_ON_YOU),
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Rebase onto dev?"}])),
                T.assistant("Still yours to decide: the api-actions pool PRs, #21840 and #21847."),
                *(T.assistant(T.tool("Read", file_path=f"api/src/f{n}.ts")) for n in range(3)),
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.user("what is the overall status of the deploy cli?"),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Hold #27868?"}])),
                T.assistant("Holding #27868 as you chose."),
                T.user(DESK_WAKE),
                T.assistant(STILL_WITH_YOU),
            ]
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.user("status?"),
                T.assistant("Desk relayed R131."),
                T.user(DESK_WAKE),
                T.assistant(STILL_WITH_YOU),
            ],
            state=[PrimitiveState(last_fired_at=2)],
        ): Block(pattern="AskUserQuestion"),
        Input(
            transcript=[
                T.assistant(O1_PROSE_DECISION),
                T.assistant(T.tool("AskUserQuestion", questions=[{"question": "Keep the IMDS firewall?"}])),
            ]
        ): Allow(),
        Input(transcript=[T.assistant("The narrow ci plan is ready; it comes to you for approval.")]): Block(
            pattern="AskUserQuestion"
        ),
        Input(transcript=[T.assistant("Applied the fix to your go-to helper; CI is green.")]): Allow(),
        Input(
            agent_id="tm1",
            transcript=[T.assistant("Holding for your pick: rebase onto dev or cherry-pick the fix?")],
        ): Allow(),
        Input(
            disallowed_tools=("AskUserQuestion", "EnterPlanMode", "ExitPlanMode"),
            transcript=[T.assistant("Holding for your pick: rebase onto dev or cherry-pick the fix?")],
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    "The gate now tells the session:\n\n```\nHolding for your pick: rebase onto dev or "
                    "cherry-pick the fix?\n```\n\nShipped #94; CI is green."
                )
            ],
            llm={"block": False},
        ): Allow(),
        Input(transcript=[T.user("status?"), T.assistant(GO_GIVEN)]): Allow(),
        Input(transcript=[T.user("what about api and restate?"), T.assistant(OWED_REPORT)]): Allow(),
        Input(transcript=[T.user(DESK_WAKE), T.assistant(APPROVAL_CONDITIONS)]): Allow(),
        Input(
            transcript=[
                T.assistant(T.tool("Skill", skill="cc-present:present")),
                T.assistant(
                    "Board is live at http://localhost:4173; if you'd rather skip the board, say 'use defaults'."
                ),
            ]
        ): Allow(),
        Input(
            transcript=[
                T.assistant(
                    T.tool(
                        "Bash",
                        command="/Users/me/.claude/plugins/cache/cc-present/bin/cc-present start --session x --doc y",
                    )
                ),
                T.assistant("Your call on the board."),
            ]
        ): Allow(),
        Input(transcript=[T.assistant("Shipped #94; CI is green.")]): Allow(),
        Input(
            transcript=[T.assistant("Both pool PRs are merged; nothing is left to decide.")],
            llm={"block": False},
        ): Allow(),
        Input(
            transcript=[T.assistant("Why did the build fail? Let me know which key went stale: the lockfile.")],
            llm={"block": False},
        ): Allow(),
    },
)
