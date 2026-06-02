from __future__ import annotations

from pydantic import BaseModel

from captain_hook import BaseHookEvent, Event, HookResult, on
from captain_hook.testing import Allow, Input


class ReviewLedger(BaseModel):
    reviewed_files: list[str] = []
    pending: bool = True


@on(
    Event.Stop,
    tests={
        Input(): Allow(),
    },
)
def require_review_before_stop(evt: BaseHookEvent) -> HookResult | None:
    ledger = evt.ctx.session[ReviewLedger].get(ReviewLedger())
    if ledger.pending and ledger.reviewed_files:
        return evt.warn("review the pending files before stopping")
    return None
