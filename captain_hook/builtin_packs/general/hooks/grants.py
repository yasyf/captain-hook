from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from captain_hook import Allow, Block, Event, Input, LambdaCondition, Tool, on
from captain_hook.grants import store
from captain_hook.grants.evidence import answer_evidence, machine_written, parse_answer, tree_of, words_evidence
from captain_hook.grants.records import Evidence, Grant

if TYPE_CHECKING:
    from captain_hook import BaseHookEvent, HookResult, PostToolUseEvent, UserPromptSubmitEvent

RECORD_TTL = timedelta(days=7)
MINTING = frozenset({"add", "import"})
PROGRAMS = ("capt-hook", "captain_hook")


def record(evt: BaseHookEvent, kind: str, item: Evidence) -> None:
    store.mint(
        Grant(
            id=store.new_id(),
            kind=kind,
            tree=tree_of(evt),
            scope={},
            evidence=[item],
            source_key=item.key,
            expires=store.expiry(RECORD_TTL),
            author=f"{kind}@{evt.session_id}/{evt.agent_id or 'main'}",
            created=store.now(),
        )
    )


@on(
    Event.PostToolUse,
    only_if=[Tool("AskUserQuestion")],
    respect_gitignore=False,
    skip_planning_agents=False,
)
def record_answers(evt: PostToolUseEvent) -> HookResult | None:
    payload = evt.tool_response
    if not isinstance(payload, dict) or (answer := parse_answer(payload)) is None or evt.tool_use_id is None:
        return None
    for item in answer_evidence(evt.tool_use_id, payload, answer, store.now()):
        record(evt, "ask", item)
    return None


@on(Event.UserPromptSubmit, respect_gitignore=False, skip_planning_agents=False)
def record_owner_words(evt: UserPromptSubmitEvent) -> HookResult | None:
    if evt.ctx.root_path is None and (prompt := evt.user_prompt) and not machine_written(prompt):
        record(evt, "words", words_evidence(prompt, store.now()))
    return None


def mints(evt: BaseHookEvent) -> bool:
    for call in evt.cmd.calls():
        words = [word.value or "" for word in call.command.words]
        if any(word.endswith(PROGRAMS) for word in words) and any(
            word == "grant" and following in MINTING for word, following in zip(words, words[1:], strict=False)
        ):
            return True
    return False


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), LambdaCondition(mints)],
    respect_gitignore=False,
    skip_planning_agents=False,
    tests={
        Input(command="capt-hook grant list"): Allow(),
        Input(command="capt-hook grant add --kind slack.write --scope k=v --quote hi"): Block(
            pattern="minted only by the owner"
        ),
        Input(command="uvx capt-hook --root /repo grant import 543e865 --kind k"): Block(
            pattern="minted only by the owner"
        ),
        Input(command="python -m captain_hook grant add --kind k --quote hi"): Block(
            pattern="minted only by the owner"
        ),
        Input(command="echo grant add"): Allow(),
    },
)
def agents_never_mint(evt: BaseHookEvent) -> HookResult | None:
    return evt.block(
        "A grant is minted only by the owner at a terminal or by a hook judging the owner's own words. Ask the"
        " owner, then retry the action itself so its hook can record their answer."
    )
